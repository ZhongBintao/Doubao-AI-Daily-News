"""v6 newsroom: pick the day's genuinely heavy AI stories from AIHOT.

Pipeline::

    gather()            fetch selected items, the daily report, hot topics,
                        all-items scores and story digests for one edition
    enrich()            second AIHOT-only pass for the strongest events: keyword
                        search for more AIHOT coverage and related stories
    build_candidates()  merge the layers per item, then cluster reports of the
                        same event and compute one heat score per event
    plan_rundown()      apply the aired ledger and let the day's content decide
                        the count: heavy events with enough material become
                        headlines (max 3), the rest above the flash line become
                        flashes (max 5), capped at an estimated 3 minutes

Only AIHOT's own API is read; original article links are never opened.

Everything here is deterministic. A writer model may later swap picks, but
only inside the pool produced here (see ``director.py``).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import config as C
from .brands import find_brand
from . import material as M
from .aihot_v2 import AIHotV2Client, AIHotV2Error

SHANGHAI = timezone(timedelta(hours=8))
DAILY_SECTION_CATEGORY = {
    "模型发布/更新": "ai-models", "产品发布/更新": "ai-products", "行业动态": "industry",
    "论文研究": "paper", "技巧与观点": "tip",
}
_ASCII_STOP = {"ai", "api", "the", "and", "for", "with", "new", "app", "llm", "gpt", "pro", "plus", "mode", "model", "agent", "agents"}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def edition_window(run_date: date, now: datetime | None = None) -> dict[str, Any]:
    """The 24h window an edition covers.

    It ends at 08:00 Beijing on ``run_date`` (the AIHOT daily cut-off) or at
    ``now`` when the edition runs earlier that morning. ``live`` editions can
    use real-time hot topics; past dates cannot (hot topics have no history).
    """

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = datetime(run_date.year, run_date.month, run_date.day, C.AIHOT_DAILY_CUTOFF_HOUR, tzinfo=SHANGHAI).astimezone(timezone.utc)
    end = min(cutoff, now) if now >= cutoff - timedelta(hours=24) else cutoff
    live = now - end <= timedelta(hours=18)
    return {"start": end - timedelta(hours=24), "end": end, "live": live, "daily_available": now >= cutoff}


def _item_id_from_link(link: Any) -> str:
    text = str(link or "").rstrip("/")
    return text.rsplit("/", 1)[-1] if "/items/" in text else ""


def _story_id_from_link(link: Any) -> str:
    text = str(link or "").rstrip("/")
    return text.rsplit("/", 1)[-1] if "/story/" in text else ""


def gather(client: AIHotV2Client, run_date: date, *, now: datetime | None = None, story_limit: int = 8) -> dict[str, Any]:
    """Freeze every AIHOT layer needed for one edition into a snapshot dict."""

    window = edition_window(run_date, now)
    snapshot: dict[str, Any] = {
        "version": C.V6_VERSION,
        "run_date": run_date.isoformat(),
        "window": {"start": window["start"].isoformat(), "end": window["end"].isoformat(), "live": window["live"]},
        "errors": [],
    }
    try:
        selected = client.items(mode="selected", window="7d")
    except AIHotV2Error as exc:
        selected = []
        snapshot["errors"].append(f"selected: {exc}")
    snapshot["selected_items"] = [
        item for item in selected
        if (t := _parse_time(item.get("discoveredAt"))) is not None and window["start"] <= t < window["end"]
    ]
    snapshot["selected_7d_index"] = {str(item.get("id")): {"score": item.get("score"), "category": item.get("category")} for item in selected}
    try:
        snapshot["daily"] = client.daily(run_date.isoformat()) if window["daily_available"] else None
    except AIHotV2Error as exc:
        snapshot["daily"] = None
        snapshot["errors"].append(f"daily: {exc}")
    snapshot["hot_topics"] = []
    snapshot["all_items_index"] = {}
    if window["live"]:
        try:
            snapshot["hot_topics"] = client.hot_topics()
        except AIHotV2Error as exc:
            snapshot["errors"].append(f"hot: {exc}")
        try:
            for item in client.items(mode="all", window="24h", max_pages=4):
                snapshot["all_items_index"][str(item.get("id"))] = {
                    "score": item.get("score"), "category": item.get("category"), "summary": item.get("summary"),
                    "source": (item.get("source") or {}).get("name"), "publishedAt": item.get("publishedAt"),
                }
        except AIHotV2Error as exc:
            snapshot["errors"].append(f"all-items: {exc}")
    stories: dict[str, Any] = {}
    for topic in snapshot["hot_topics"][:story_limit]:
        story_id = _story_id_from_link((topic.get("links") or {}).get("story"))
        if story_id and story_id not in stories:
            try:
                story = client.story(story_id)
            except AIHotV2Error as exc:
                snapshot["errors"].append(f"story {story_id}: {exc}")
                story = None
            if story:
                stories[story_id] = story
    snapshot["stories"] = stories
    return snapshot


def enrich(client: AIHotV2Client, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Second, AIHOT-only fetch pass for the strongest events (mutates ``snapshot``).

    * keyword search over all public AIHOT items of the last 7 days, so an event
      gets every AIHOT summary written about it (not only the selected one);
    * AIHOT "related stories" of the top hot events, used as labelled background.

    Results are frozen into the snapshot, so selection stays reproducible.
    """

    searches: dict[str, Any] = snapshot.setdefault("searches", {})
    search_for: dict[str, list[str]] = snapshot.setdefault("search_for", {})
    related: dict[str, Any] = snapshot.setdefault("related_stories", {})
    stories = snapshot.get("stories") or {}
    clusters = cluster_candidates(build_candidates(snapshot))
    related_events = 0
    for cluster in clusters[: C.ENRICH_CLUSTERS]:
        lead = cluster.lead
        queries: list[str] = []
        for query in M.search_queries(lead.title, lead.hot_title):
            if query not in searches:
                try:
                    searches[query] = [M.trim_item(i) for i in client.search(query, limit=C.SEARCH_LIMIT)]
                except AIHotV2Error as exc:
                    snapshot["errors"].append(f"search {query}: {exc}")
                    searches[query] = []
            queries.append(query)
            if len(searches[query]) >= 3:
                break  # the specific query already found coverage; skip the broad one
        search_for[lead.item_id] = queries
        story = next((stories[m.story_id] for m in cluster.members if m.story_id and m.story_id in stories), None)
        if story and related_events < C.ENRICH_RELATED_EVENTS:
            related_events += 1
            for ref in (story.get("related") or [])[: C.ENRICH_RELATED_PER_EVENT]:
                public_id = str(ref.get("publicId") or "")
                if not public_id or public_id in related or public_id in stories:
                    continue
                try:
                    rel = client.story(public_id)
                except AIHotV2Error as exc:
                    snapshot["errors"].append(f"related story {public_id}: {exc}")
                    rel = None
                if rel:
                    related[public_id] = M.trim_story(rel)
    return snapshot


# ---------------------------------------------------------------------------
# candidates and clustering
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    item_id: str
    title: str
    summary: str = ""
    category: str = "other"
    source: str = ""
    aihot_url: str = ""
    original_url: str = ""
    published_at: str = ""
    score: float | None = None
    in_selected: bool = False
    in_daily: bool = False
    daily_lead: bool = False
    daily_flash: bool = False
    hot_rank: int | None = None
    hot_title: str = ""
    source_count: int = 0
    participant_count: int = 0
    story_id: str = ""
    digest: str = ""
    first_report_at: str = ""
    stale: bool = False
    heat: float = 0.0
    heat_parts: dict[str, float] = field(default_factory=dict)

    def material(self) -> str:
        return "\n".join(part for part in (self.title, self.hot_title, self.summary, self.digest) if part)


def _candidate_heat(c: Candidate) -> tuple[float, dict[str, float]]:
    parts: dict[str, float] = {"score": float(c.score) if isinstance(c.score, (int, float)) else float(C.HEAT_DEFAULT_SCORE)}
    if c.in_daily:
        parts["daily"] = C.HEAT_DAILY_BONUS
    if c.daily_lead:
        parts["daily_lead"] = C.HEAT_DAILY_LEAD_BONUS
    if c.daily_flash:
        parts["daily_flash"] = C.HEAT_DAILY_FLASH_BONUS
    if c.hot_rank:
        parts["hot_rank"] = max(0, C.HEAT_HOT_RANK_BASE - C.HEAT_HOT_RANK_STEP * c.hot_rank)
        parts["hot_sources"] = C.HEAT_HOT_SOURCE_BONUS * min(c.source_count, C.HEAT_HOT_SOURCE_CAP)
    if c.stale:
        parts["stale_event"] = -C.HEAT_STALE_EVENT_PENALTY
    parts["category"] = C.HEAT_CATEGORY_ADJUST.get(c.category, C.HEAT_CATEGORY_ADJUST["other"])
    return round(sum(parts.values()), 2), parts


def _infer_category(text: str) -> str:
    if re.search(r"发布.{0,12}模型|模型.{0,6}(发布|开源)|开源.{0,8}模型|基准|多模态模型", text):
        return "ai-models"
    if re.search(r"上线|推出|开放|功能|额度|限额|套餐|App|应用", text):
        return "ai-products"
    if re.search(r"研究|论文|报告|提出", text):
        return "paper"
    if re.search(r"融资|上市|IPO|S-1|营收|收购|投资|股价|起诉|监管", text):
        return "industry"
    return "other"


def build_candidates(snapshot: Mapping[str, Any]) -> list[Candidate]:
    pool: dict[str, Candidate] = {}
    seven_day = snapshot.get("selected_7d_index") or {}
    all_index = snapshot.get("all_items_index") or {}

    def upsert(item_id: str, **fields: Any) -> Candidate:
        c = pool.get(item_id)
        if c is None:
            c = Candidate(item_id=item_id, title=str(fields.get("title") or ""))
            pool[item_id] = c
        for key, value in fields.items():
            if value in (None, "", 0, False):
                continue
            if key in {"title", "summary", "source"} and getattr(c, key):
                continue
            setattr(c, key, value)
        return c

    for item in snapshot.get("selected_items") or []:
        links = item.get("links") or {}
        upsert(str(item.get("id")), title=item.get("title"), summary=item.get("summary") or "", category=item.get("category") or "other",
               source=(item.get("source") or {}).get("name", ""), aihot_url=links.get("aihot", ""), original_url=links.get("original", ""),
               published_at=item.get("publishedAt") or item.get("discoveredAt") or "", score=item.get("score"), in_selected=True)
    daily = snapshot.get("daily") or {}
    lead_title = str((daily.get("lead") or {}).get("title") or "")
    for section in daily.get("sections") or []:
        section_category = DAILY_SECTION_CATEGORY.get(str(section.get("label") or ""), "")
        for item in section.get("items") or []:
            links = item.get("links") or {}
            item_id = _item_id_from_link(links.get("aihot"))
            if not item_id:
                continue
            known = seven_day.get(item_id) or all_index.get(item_id) or {}
            c = upsert(item_id, title=item.get("title"), summary=item.get("summary") or "", source=(item.get("source") or {}).get("name", ""),
                       aihot_url=links.get("aihot", ""), original_url=links.get("original", ""), in_daily=True,
                       category=known.get("category") or section_category or "other", score=known.get("score"))
            if str(item.get("title") or "") == lead_title:
                c.daily_lead = True
    for item in daily.get("flashes") or []:
        links = item.get("links") or {}
        item_id = _item_id_from_link(links.get("aihot"))
        if item_id:
            known = seven_day.get(item_id) or all_index.get(item_id) or {}
            upsert(item_id, title=item.get("title"), source=(item.get("source") or {}).get("name", ""), aihot_url=links.get("aihot", ""),
                   original_url=links.get("original", ""), daily_flash=True, category=known.get("category"), score=known.get("score"))
    stories = snapshot.get("stories") or {}
    for topic in snapshot.get("hot_topics") or []:
        links = topic.get("links") or {}
        item_id = str(topic.get("id") or "")
        story_id = _story_id_from_link(links.get("story"))
        story = stories.get(story_id) or {}
        known = seven_day.get(item_id) or all_index.get(item_id) or {}
        summary = known.get("summary") or story.get("latest") or ""
        c = upsert(item_id, title=topic.get("title"), summary=summary, source=(topic.get("source") or {}).get("name", ""),
                   aihot_url=links.get("aihot", ""), original_url=links.get("original", ""), published_at=topic.get("latestAt") or "",
                   score=known.get("score"), category=known.get("category"))
        c.hot_rank = int(topic.get("rank") or 0) or None
        c.hot_title = str(topic.get("title") or "")
        c.source_count = int(story.get("sourceCount") or topic.get("sourceCount") or 0)
        c.participant_count = int(topic.get("participantCount") or 0)
        c.story_id = story_id
        c.digest = str(story.get("digest") or "")
        c.first_report_at = str(story.get("firstReportAt") or "")
        first = _parse_time(c.first_report_at)
        window_start = _parse_time((snapshot.get("window") or {}).get("start"))
        c.stale = bool(first and window_start and first < window_start - timedelta(hours=C.HEAT_STALE_EVENT_HOURS))
    for c in pool.values():
        if not c.category or c.category == "other":
            c.category = _infer_category(c.material())
        c.heat, c.heat_parts = _candidate_heat(c)
    return sorted(pool.values(), key=lambda c: (-c.heat, c.item_id))


def _ascii_tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{1,}", text) if t.lower() not in _ASCII_STOP}


def _bigrams(text: str) -> set[str]:
    han = re.sub(r"[^一-鿿]", "", text)
    return {han[i:i + 2] for i in range(len(han) - 1)}


def same_event(a_title: str, b_title: str) -> bool:
    """Conservative title-level duplicate test (AIHOT story ids are preferred)."""

    a_ascii, b_ascii = _ascii_tokens(a_title), _ascii_tokens(b_title)
    a_bi, b_bi = _bigrams(a_title), _bigrams(b_title)
    jaccard = len(a_bi & b_bi) / max(1, len(a_bi | b_bi))
    shared_ascii = a_ascii & b_ascii
    if shared_ascii and (jaccard >= 0.2 or len(shared_ascii) >= 2):
        return True
    return jaccard >= 0.45


@dataclass
class EventCluster:
    key: str
    lead: Candidate
    members: list[Candidate]
    heat: float
    aired: bool = False
    aired_reason: str = ""
    pack: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "heat": self.heat, "aired": self.aired, "aired_reason": self.aired_reason,
            "lead": candidate_dict(self.lead), "member_ids": [m.item_id for m in self.members],
            "member_titles": [m.title for m in self.members],
            "pack": self.pack, "budget": self.budget or M.story_budget(self.pack),
        }


def candidate_dict(c: Candidate) -> dict[str, Any]:
    return {
        "item_id": c.item_id, "title": c.title, "hot_title": c.hot_title, "summary": c.summary, "digest": c.digest,
        "category": c.category, "category_label": C.V6_CATEGORY_LABELS.get(c.category, "AI 动态"), "source": c.source,
        "aihot_url": c.aihot_url, "original_url": c.original_url, "published_at": c.published_at,
        "score": c.score, "heat": c.heat, "heat_parts": c.heat_parts,
        "signals": {"selected": c.in_selected, "daily": c.in_daily, "daily_lead": c.daily_lead, "daily_flash": c.daily_flash,
                    "hot_rank": c.hot_rank, "source_count": c.source_count, "participant_count": c.participant_count},
        "story_id": c.story_id, "first_report_at": c.first_report_at, "stale": c.stale,
    }


def cluster_candidates(candidates: Iterable[Candidate]) -> list[EventCluster]:
    clusters: list[EventCluster] = []
    for c in sorted(candidates, key=lambda c: (-c.heat, c.item_id)):
        target = None
        for cluster in clusters:
            if (c.story_id and any(m.story_id == c.story_id for m in cluster.members)) or any(
                same_event(c.title, m.title) or (c.hot_title and same_event(c.hot_title, m.title)) for m in cluster.members
            ):
                target = cluster
                break
        if target is None:
            clusters.append(EventCluster(key=c.story_id or c.item_id, lead=c, members=[c], heat=c.heat))
        else:
            target.members.append(c)
    for cluster in clusters:
        # Merge signals of all reports into the lead so the event scores once.
        lead = cluster.lead
        for m in cluster.members[1:]:
            lead.in_daily = lead.in_daily or m.in_daily
            lead.daily_lead = lead.daily_lead or m.daily_lead
            lead.daily_flash = lead.daily_flash or m.daily_flash
            if m.hot_rank and (not lead.hot_rank or m.hot_rank < lead.hot_rank):
                lead.hot_rank, lead.hot_title, lead.story_id = m.hot_rank, m.hot_title, m.story_id or lead.story_id
                lead.source_count, lead.participant_count = m.source_count, m.participant_count
                lead.digest = lead.digest or m.digest
            if not lead.summary and m.summary:
                lead.summary = m.summary
        lead.heat, lead.heat_parts = _candidate_heat(lead)
        extra = min(6, 2 * (len(cluster.members) - 1))
        if extra:
            lead.heat_parts["coverage"] = extra
            lead.heat = round(lead.heat + extra, 2)
        cluster.heat = lead.heat
    return sorted(clusters, key=lambda k: (-k.heat, k.key))


def _cluster_story(cluster: EventCluster, stories: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next((stories[m.story_id] for m in cluster.members if m.story_id and m.story_id in stories), None)


def _linked(a: EventCluster, b: EventCluster, stories: Mapping[str, Any]) -> bool:
    """True when AIHOT's story of ``a`` covers ``b``: a related story, or one of its per-source reports."""

    story = _cluster_story(a, stories)
    if not story:
        return False
    refs = story.get("related") or []
    ref_ids = {str(r.get("publicId") or "") for r in refs}
    if any(m.story_id and m.story_id in ref_ids for m in b.members):
        return True
    if any(same_event(str(r.get("title") or ""), m.title) for r in refs for m in b.members):
        return True
    for report in story.get("reports") or []:
        text = f"{report.get('title') or ''} {report.get('summary') or ''}"
        for m in b.members:
            if same_event(str(report.get("title") or ""), m.title) or (m.summary and M.near_duplicate(m.summary, [text], 0.42)):
                return True
    return False


def merge_related_events(clusters: list[EventCluster], snapshot: Mapping[str, Any]) -> list[EventCluster]:
    """Fold events of one storyline into the strongest of them.

    AIHOT often splits one storyline (an IPO filing, its revenue figures, its
    compute commitments) into separate events. Two events merge when they
    share a subject and AIHOT's story of either one lists the other as a
    related story or carries it among its per-source reports. Telling them as
    one story is deeper and avoids repeating the subject in several stories.
    """

    stories = snapshot.get("stories") or {}
    current = list(clusters)
    while True:  # a merge can link a host to further events, so repeat until stable
        merged = _merge_pass(current, stories)
        if len(merged) == len(current):
            return merged
        current = merged


def _merge_pass(clusters: list[EventCluster], stories: Mapping[str, Any]) -> list[EventCluster]:
    merged: list[EventCluster] = []
    for cluster in sorted(clusters, key=lambda k: (-k.heat, k.key)):
        subject = find_subject(cluster.lead.hot_title or cluster.lead.title).lower()
        target = None
        for host in merged:
            if subject and find_subject(host.lead.hot_title or host.lead.title).lower() == subject and (
                _linked(host, cluster, stories) or _linked(cluster, host, stories)
            ):
                target = host
                break
        if target is None:
            merged.append(cluster)
            continue
        target.members.extend(cluster.members)
        lead = target.lead
        for m in cluster.members:  # keep the strongest signals of the storyline on the lead
            lead.in_daily, lead.daily_lead = lead.in_daily or m.in_daily, lead.daily_lead or m.daily_lead
            if m.story_id and not lead.story_id:
                lead.story_id, lead.digest, lead.source_count = m.story_id, m.digest, m.source_count
                lead.hot_rank, lead.hot_title = lead.hot_rank or m.hot_rank, lead.hot_title or m.hot_title
        lead.heat, lead.heat_parts = _candidate_heat(lead)
        lead.heat_parts["coverage"] = min(6, 2 * (len(target.members) - 1))
        target.heat = lead.heat = round(sum(lead.heat_parts.values()), 2)
    return sorted(merged, key=lambda k: (-k.heat, k.key))


def find_subject(title: str) -> str:
    brand = find_brand(title or "")
    return str((brand or {}).get("name") or "")


# ---------------------------------------------------------------------------
# aired ledger
# ---------------------------------------------------------------------------
def load_aired(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [entry for entry in data.get("entries") or [] if isinstance(entry, Mapping)]


def record_aired(path: Path, run_date: date, stories: Iterable[Mapping[str, Any]]) -> None:
    entries = [e for e in load_aired(path) if e.get("date") != run_date.isoformat()]
    for story in stories:
        entries.append({"date": run_date.isoformat(), "item_ids": list(story.get("item_ids") or []), "story_id": story.get("story_id") or "",
                        "title": story.get("title") or "", "role": story.get("role") or ""})
    cutoff = (run_date - timedelta(days=30)).isoformat()
    entries = [e for e in entries if str(e.get("date") or "") >= cutoff]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")


def reconstruct_aired(client: AIHotV2Client, run_date: date, days: int = C.AIRED_LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """Approximate the aired ledger when it was lost (e.g. cloud reset).

    Uses the previous days' AIHOT daily leads, the most likely headlines.
    """

    entries: list[dict[str, Any]] = []
    for offset in range(1, days + 1):
        day = run_date - timedelta(days=offset)
        try:
            report = client.daily(day.isoformat())
        except AIHotV2Error:
            report = None
        lead = (report or {}).get("lead") or {}
        if lead.get("title"):
            entries.append({"date": day.isoformat(), "item_ids": [], "story_id": "", "title": lead["title"], "role": "reconstructed-daily-lead"})
    return entries


def mark_aired(clusters: list[EventCluster], aired: Iterable[Mapping[str, Any]], run_date: date) -> None:
    recent = [e for e in aired if str(e.get("date") or "") >= (run_date - timedelta(days=C.AIRED_LOOKBACK_DAYS)).isoformat() and str(e.get("date") or "") < run_date.isoformat()]
    for cluster in clusters:
        for entry in recent:
            ids = set(entry.get("item_ids") or [])
            hit = bool(ids & {m.item_id for m in cluster.members}) or (entry.get("story_id") and any(m.story_id == entry.get("story_id") for m in cluster.members))
            hit = hit or any(same_event(str(entry.get("title") or ""), m.title) for m in cluster.members)
            if hit:
                cluster.aired = True
                cluster.aired_reason = f"{entry.get('date')} 已播：{entry.get('title')}"
                cluster.heat = round(cluster.heat - C.HEAT_AIRED_PENALTY, 2)
                cluster.lead.heat_parts["aired"] = -C.HEAT_AIRED_PENALTY
                cluster.lead.heat = cluster.heat
                break


# ---------------------------------------------------------------------------
# rundown
# ---------------------------------------------------------------------------
def plan_rundown(clusters: list[EventCluster]) -> dict[str, Any]:
    """Let the day's content decide how many stories run.

    * headline: heavy (>= HEAT_HEAVY_THRESHOLD), not aired, and with enough AIHOT
      material to tell it in depth; at most RUNDOWN_MAX_HEADLINES;
    * flash: everything else above HEAT_FLASH_THRESHOLD (heavy-but-thin events
      included); at most RUNDOWN_MAX_FLASHES, or RUNDOWN_QUIET_FLASHES on a quiet day;
    * there is no overall time cap: the caps on headlines and flashes and the
      per-story budgets bound the length (``estimated_seconds`` is informational).

    Thresholds use the raw heat; ranking inside them uses heat + a material
    bonus (``RUNDOWN_DEPTH_BONUS``), so when two events are close in heat the
    one with more AIHOT material wins the slot.
    """

    ranked = sorted(clusters, key=lambda k: (-k.heat, k.key))
    usable = [k for k in ranked if k.lead.title]
    if not usable:
        return {"mode": "no-news", "headlines": [], "flashes": [], "pool": [], "heavy_count": 0, "dropped": [], "estimated_seconds": 0.0}
    for k in usable:
        k.budget = k.budget or M.story_budget(k.pack)
    fresh = [k for k in usable if not k.aired]

    def merit(k: EventCluster) -> tuple[float, str]:
        return (-(k.heat + C.RUNDOWN_DEPTH_BONUS.get(str(k.budget.get("depth")), 0)), k.key)

    heavy = [k for k in fresh if k.heat >= C.HEAT_HEAVY_THRESHOLD]
    headlines = sorted([k for k in heavy if k.budget["can_headline"]], key=merit)[: C.RUNDOWN_MAX_HEADLINES]
    mode = "multi" if len(headlines) >= 2 else "normal"
    if not headlines:
        # Quiet day: one light headline, preferring an event that has material to tell.
        top = (fresh or usable)[:3]
        headlines = [next((k for k in top if k.budget["can_headline"]), top[0])]
        mode = "quiet"
    flash_cap = C.RUNDOWN_QUIET_FLASHES if mode == "quiet" else C.RUNDOWN_MAX_FLASHES
    flashes = sorted([k for k in fresh if k not in headlines and k.heat >= C.HEAT_FLASH_THRESHOLD], key=merit)[:flash_cap]
    dropped: list[dict[str, Any]] = []

    def estimate() -> float:
        return round(4.0 + 3.0 + sum(M.estimate_seconds(k.budget, "headline") for k in headlines)
                     + sum(M.estimate_seconds(k.budget, "flash") for k in flashes), 1)

    pool = [k for k in fresh][: C.CANDIDATE_POOL_SIZE]
    for k in headlines + flashes:
        if k not in pool:
            pool.append(k)
    return {"mode": mode, "heavy_count": len(heavy), "headlines": headlines, "flashes": flashes, "pool": pool,
            "dropped": dropped, "estimated_seconds": estimate()}


def select_edition(snapshot: Mapping[str, Any], aired: Iterable[Mapping[str, Any]], run_date: date) -> dict[str, Any]:
    """Full deterministic selection report written to artifacts/selection.json."""

    clusters = merge_related_events(cluster_candidates(build_candidates(snapshot)), snapshot)
    for cluster in clusters:
        cluster.pack = M.build_pack(cluster, snapshot)
        cluster.budget = M.story_budget(cluster.pack)
    mark_aired(clusters, aired, run_date)
    rundown = plan_rundown(clusters)
    return {
        "version": C.V6_VERSION,
        "run_date": run_date.isoformat(),
        "window": snapshot.get("window"),
        "mode": rundown["mode"],
        "heavy_count": rundown["heavy_count"],
        "thresholds": {"heavy": C.HEAT_HEAVY_THRESHOLD, "flash": C.HEAT_FLASH_THRESHOLD},
        "limits": {"headlines": C.RUNDOWN_MAX_HEADLINES, "flashes": C.RUNDOWN_MAX_FLASHES},
        "estimated_seconds": rundown["estimated_seconds"],
        "dropped": rundown["dropped"],
        "recommended": {
            "headlines": [k.lead.item_id for k in rundown["headlines"]],
            "flashes": [k.lead.item_id for k in rundown["flashes"]],
        },
        "pool": [k.to_dict() for k in rundown["pool"]],
        "clusters": [k.to_dict() for k in clusters],
        "sources_used": {
            "selected_items": len(snapshot.get("selected_items") or []),
            "daily": bool(snapshot.get("daily")),
            "hot_topics": len(snapshot.get("hot_topics") or []),
            "stories": len(snapshot.get("stories") or {}),
            "searches": len(snapshot.get("searches") or {}),
            "related_stories": len(snapshot.get("related_stories") or {}),
        },
        "errors": list(snapshot.get("errors") or []),
    }
