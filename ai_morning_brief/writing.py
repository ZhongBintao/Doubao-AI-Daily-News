from __future__ import annotations

"""Editorial writing and deterministic speech normalization.

The renderer intentionally does not call a language model.  A Codex writing
skill authors or reviews ``editorial_plan.json`` against the frozen AIHOT
input, while this module provides the repeatable, machine-checkable part of
the hand-off: richer draft scaffolding, display/spoken text separation, and a
pronunciation ledger for Azure (and future providers).
"""

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping


WRITER_VERSION = "4.1"
WRITER_PROMPT_VERSION = "codex-news-writer-v4.1"
SPEECH_NORMALIZATION_VERSION = "2.0"
CAPTION_MAX_VISIBLE_UNITS = 28
CAPTION_MIN_STORIES = 3


class WritingError(ValueError):
    """Raised when a writer artifact cannot be made safe for narration."""


@dataclass(frozen=True)
class SpeechNormalization:
    display_text: str
    spoken_text: str
    rewrites: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = tuple()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SPEECH_NORMALIZATION_VERSION,
            "display_text": self.display_text,
            "spoken_text": self.spoken_text,
            "rewrites": [dict(item) for item in self.rewrites],
            "warnings": list(self.warnings),
        }


_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d[\d,，]*(?:\.\d+)?")
_GROUPED_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d{1,3}(?:[,，]\d{3})+)(?!\d)")
_RATE_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>tokens?|token)\s*/\s*s\b", re.IGNORECASE)
_BYTE_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>Gi?B|Mi?B|Ki?B|Ti?B|GB|MB|KB|TB)\b", re.IGNORECASE)
_BILLION_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>bn|billion|B)\b")
_MILLION_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>mn|million|M)\b")
_THOUSAND_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>K|k)\b")
_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>tokens?|token)\b", re.IGNORECASE)
_PLAIN_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d[\d,，]*(?:\.\d+)?)(?![A-Za-z0-9])")
_COUNT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<number>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>位|项|名|个|家|页|类|条|种|款|倍|分钟|分|秒|月|日|年|%|％|亿美元|美元|亿元|万元)(?![A-Za-z])"
)
_QUANTIZED_RE = re.compile(r"(?<![A-Za-z0-9])(?P<letters>[A-Za-z])(?P<number>\d+)(?P<tail>(?:_[A-Za-z0-9]+)+)\b")
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?P<code>[A-Z]{2,6}(?:[-_][A-Z0-9]{1,8})+)\b")
_MODEL_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])(?P<name>[A-Za-z]{2,})-(?P<number>\d+(?:\.\d+)?)(?P<suffix>[A-Za-z]*)\b")
_MIXED_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])(?P<name>[A-Za-z]{2,})(?P<number>\d+(?:\.\d+)?)(?![A-Za-z0-9])")
_LOCALE_RE = re.compile(r"(?<![A-Za-z])(?P<language>[a-z]{2})-(?P<region>[A-Z]{2})(?![A-Za-z])")
_ACRONYM_RE = re.compile(r"(?<![A-Za-z])(?P<acronym>[A-Z]{2,5})(?![A-Za-z])")

_ZH_DIGITS = "零一二三四五六七八九"
_ZH_SMALL_UNITS = ("", "十", "百", "千")
_ZH_GROUP_UNITS = ("", "万", "亿", "兆")

# Terms which should remain recognizable to a bilingual audience.  They are
# surrounded with spaces when spoken so Azure reads the letters separately
# rather than guessing a word (for example ``ASR`` as one syllable).
_KEEP_AS_WORD = {
    "AI",
    "API",
    "Azure",
    "Gemini",
    "Google",
    "Mac",
    "OpenAI",
    "OpenMontage",
    "Ollama",
    "Qwen",
    "Studio",
    "token",
    "tokens",
}


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\u3000", " ").strip())


def normalize_display_text(text: Any) -> str:
    """Canonicalize authored visible copy without changing its facts.

    AIHOT sometimes uses thousands separators inside Chinese prose (for
    example ``4，888``).  Captions and cards use ungrouped integers so the
    viewer sees one stable token; the pronunciation normalizer handles the
    spoken reading separately.
    """

    value = _clean(text)
    return _GROUPED_NUMBER_RE.sub(lambda match: match.group("number").replace(",", "").replace("，", ""), value)


def caption_visible_units(text: Any) -> int:
    """Return the single-line caption weight used by the renderer."""

    value = normalize_display_text(text)
    total = 0
    index = 0
    while index < len(value):
        if value[index].isspace():
            index += 1
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9.+#/-]*", value[index:])
        if match:
            total += max(1, len(match.group(0)))
            index += len(match.group(0))
        else:
            total += 1
            index += 1
    return total


def split_caption_sentences(text: Any) -> list[str]:
    """Split authored copy only at terminal punctuation."""

    value = normalize_display_text(text)
    matches = list(re.finditer(r"[^。！？!?；;]+[。！？!?；;]", value))
    parts = [match.group(0).strip() for match in matches]
    cursor = matches[-1].end() if matches else 0
    remainder = value[cursor:].strip()
    if remainder:
        parts.append(remainder)
    return [part for part in parts if part]


def split_caption_units(text: Any, maximum: int = CAPTION_MAX_VISIBLE_UNITS) -> list[str]:
    """Split complete editorial prose into readable subtitle-sized units.

    Editorial beats describe a complete fact context and are deliberately not
    constrained by caption width.  This function performs the presentation
    split afterwards, preferring sentence and clause boundaries while
    preserving the authored text byte-for-byte apart from canonical spacing.
    """

    value = normalize_display_text(text)
    if not value:
        return []
    pending = split_caption_sentences(value)
    units: list[str] = []
    soft_breaks = set("，、：；,;:）)")
    for sentence in pending:
        remaining = sentence
        while remaining:
            if caption_visible_units(remaining) <= maximum:
                units.append(remaining)
                break
            fit = 0
            for index in range(1, len(remaining) + 1):
                if caption_visible_units(remaining[:index]) > maximum:
                    break
                fit = index
            fit = max(1, fit)
            preferred = 0
            lower_bound = max(1, fit // 2)
            for index in range(lower_bound, fit + 1):
                if remaining[index - 1] in soft_breaks or remaining[index - 1].isspace():
                    preferred = index
            split_at = preferred or fit
            units.append(remaining[:split_at])
            remaining = remaining[split_at:]
    return [unit for unit in units if unit.strip()]


def _number_to_zh_integer(value: int) -> str:
    if value == 0:
        return _ZH_DIGITS[0]
    if value < 0:
        return "负" + _number_to_zh_integer(-value)
    groups: list[int] = []
    while value:
        groups.append(value % 10000)
        value //= 10000
    output: list[str] = []
    pending_zero = False
    for group_index in range(len(groups) - 1, -1, -1):
        group = groups[group_index]
        if group == 0:
            if output:
                pending_zero = True
            continue
        if output and (pending_zero or group < 1000):
            if output[-1] != _ZH_DIGITS[0]:
                output.append(_ZH_DIGITS[0])
        pending_zero = False
        digits: list[str] = []
        divisor = 1000
        started = False
        for unit_index in range(3, -1, -1):
            digit = (group // divisor) % 10
            divisor //= 10
            if digit:
                digits.append(_ZH_DIGITS[digit] + _ZH_SMALL_UNITS[unit_index])
                started = True
            elif started and any((group // (10 ** power)) % 10 for power in range(unit_index)):
                if not digits or digits[-1] != _ZH_DIGITS[0]:
                    digits.append(_ZH_DIGITS[0])
        while digits and digits[-1] == _ZH_DIGITS[0]:
            digits.pop()
        output.extend(digits)
        output.append(_ZH_GROUP_UNITS[group_index])
    result = "".join(output).replace("零零", "零")
    if result.startswith("一十"):
        result = result[1:]
    return result.rstrip("零") or _ZH_DIGITS[0]


def _number_to_zh(value: str) -> str:
    raw = str(value).replace(",", "").replace("，", "").strip()
    if "." not in raw:
        try:
            return _number_to_zh_integer(int(raw))
        except ValueError:
            return raw
    whole, decimal = raw.split(".", 1)
    try:
        whole_text = _number_to_zh_integer(int(whole))
    except ValueError:
        whole_text = whole
    return whole_text + "点" + "".join(_ZH_DIGITS[int(char)] if char.isdigit() else char for char in decimal)


def _scaled_financial_number(value: str, multiplier: int) -> str:
    """Read a B/M/K suffix in Chinese without losing its magnitude."""

    raw = str(value).replace(",", "")
    try:
        if "." in raw:
            scaled = float(raw) * multiplier
            if scaled.is_integer():
                return _number_to_zh_integer(int(scaled))
            # Keep a decimal only when the scaled value cannot be represented
            # as an integer (e.g. 0.5B -> 五亿 is still exact).
            return _number_to_zh(str(scaled))
        return _number_to_zh_integer(int(raw) * multiplier)
    except (TypeError, ValueError):
        return raw


def _replace_with_ledger(text: str, pattern: re.Pattern[str], callback: Any, rewrites: list[dict[str, Any]], kind: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        source = match.group(0)
        spoken = str(callback(match))
        if source != spoken:
            rewrites.append({"source": source, "spoken": spoken, "kind": kind, "start": match.start(), "end": match.end()})
        return spoken

    return pattern.sub(replacement, text)


def normalize_spoken_text(text: str) -> str:
    """Return a pronunciation-safe Mandarin variant of authored display text.

    The function is deliberately conservative: it rewrites only patterns with
    a known pronunciation hazard and leaves ordinary Chinese prose untouched.
    """

    return normalize_with_ledger(text).spoken_text


def normalize_with_ledger(text: str) -> SpeechNormalization:
    raw_display = _clean(text)
    display = normalize_display_text(raw_display)
    if not display:
        return SpeechNormalization(display_text="", spoken_text="", rewrites=tuple(), warnings=("empty_text",))
    spoken = display
    rewrites: list[dict[str, Any]] = []

    for match in _GROUPED_NUMBER_RE.finditer(raw_display):
        source = match.group("number")
        canonical = source.replace(",", "").replace("，", "")
        rewrites.append(
            {
                "source": source,
                "spoken": canonical,
                "kind": "display_number_separator",
                "start": match.start(),
                "end": match.end(),
            }
        )

    spoken = _replace_with_ledger(
        spoken,
        _RATE_RE,
        lambda match: f"每秒{_number_to_zh(match.group('number'))}个 token",
        rewrites,
        "rate",
    )
    spoken = _replace_with_ledger(
        spoken,
        _BYTE_RE,
        lambda match: f"{_number_to_zh(match.group('number'))}{ {'KB': '千字节', 'KIB': '千字节', 'MB': '兆字节', 'MIB': '兆字节', 'GB': '吉字节', 'GIB': '吉字节', 'TB': '太字节', 'TIB': '太字节'}.get(match.group('unit').upper(), match.group('unit')) }",
        rewrites,
        "storage_unit",
    )
    spoken = _replace_with_ledger(
        spoken,
        _BILLION_RE,
        lambda match: _scaled_financial_number(match.group('number'), 1000000000),
        rewrites,
        "billion_unit",
    )
    spoken = _replace_with_ledger(
        spoken,
        _MILLION_RE,
        lambda match: _scaled_financial_number(match.group('number'), 1000000),
        rewrites,
        "million_unit",
    )
    spoken = _replace_with_ledger(
        spoken,
        _THOUSAND_RE,
        lambda match: _scaled_financial_number(match.group('number'), 1000),
        rewrites,
        "thousand_unit",
    )
    spoken = _replace_with_ledger(
        spoken,
        _NUMBER_TOKEN_RE,
        lambda match: f"{_number_to_zh(match.group('number'))}个 token",
        rewrites,
        "token_count",
    )
    spoken = _replace_with_ledger(
        spoken,
        _COUNT_RE,
        lambda match: (
            f"百分之{_number_to_zh(match.group('number'))}"
            if match.group("unit") in {"%", "％"}
            else f"{_number_to_zh(match.group('number'))}{match.group('unit')}"
        ),
        rewrites,
        "count_or_measure",
    )
    spoken = _replace_with_ledger(
        spoken,
        _MODEL_VERSION_RE,
        lambda match: f"{match.group('name')} {_number_to_zh(match.group('number'))} " + " ".join(match.group('suffix')) if match.group('suffix') else f"{match.group('name')} {_number_to_zh(match.group('number'))}",
        rewrites,
        "hyphenated_model_version",
    )
    spoken = _replace_with_ledger(
        spoken,
        _MIXED_VERSION_RE,
        lambda match: f"{match.group('name')} {_number_to_zh(match.group('number'))}",
        rewrites,
        "mixed_model_version",
    )
    spoken = _replace_with_ledger(
        spoken,
        _QUANTIZED_RE,
        lambda match: f"{match.group('letters').upper()} {_number_to_zh(match.group('number'))} " + " ".join(match.group('tail').lstrip("_").split("_")),
        rewrites,
        "quantization_code",
    )
    # Break slash/underscore/hyphenated all-caps codes only after the specific
    # rate and quantization rules above have had first refusal.
    spoken = _replace_with_ledger(
        spoken,
        _CODE_RE,
        lambda match: " ".join(re.split(r"[-_]", match.group('code'))),
        rewrites,
        "compound_code",
    )
    spoken = _replace_with_ledger(
        spoken,
        _LOCALE_RE,
        lambda match: f"{match.group('language')} {match.group('region')}",
        rewrites,
        "locale_code",
    )
    spoken = _replace_with_ledger(
        spoken,
        re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)/([A-Za-z][A-Za-z0-9]*)\b"),
        lambda match: f"{match.group(1)} 每 {match.group(2)}",
        rewrites,
        "slash_code",
    )
    spoken = _replace_with_ledger(
        spoken,
        _PLAIN_NUMBER_RE,
        lambda match: _number_to_zh(match.group("number")),
        rewrites,
        "plain_number",
    )

    def acronym(match: re.Match[str]) -> str:
        value = match.group("acronym")
        if value in _KEEP_AS_WORD:
            return value
        return " ".join(value)

    spoken = _replace_with_ledger(spoken, _ACRONYM_RE, acronym, rewrites, "acronym")
    spoken = re.sub(r"\s+([，。！？；：,.!?;:])", r"\1", spoken)
    spoken = re.sub(r"([，。！？；：,.!?;:]){2,}", r"\1", spoken)
    spoken = _clean(spoken)
    warnings: list[str] = []
    if re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", spoken):
        warnings.append("underscore_remains")
    if re.search(r"[A-Za-z0-9]/[A-Za-z0-9]", spoken):
        warnings.append("slash_remains")
    return SpeechNormalization(display_text=display, spoken_text=spoken, rewrites=tuple(rewrites), warnings=tuple(warnings))


def build_pronunciation_ledger(script: Mapping[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for segment in script.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        display = str(segment.get("display_text") or segment.get("broadcast_text") or "")
        spoken = str(segment.get("spoken_text") or segment.get("broadcast_text") or "")
        result = normalize_with_ledger(display)
        records.append({
            "segment_id": str(segment.get("id") or ""),
            "display_text": display,
            "spoken_text": spoken,
            "expected_normalized_text": result.spoken_text,
            "rewrites": [dict(item) for item in result.rewrites],
            "warnings": list(result.warnings),
            "status": "passed" if spoken == result.spoken_text and not result.warnings else "review",
        })
    errors = [
        f"{record['segment_id']}: {','.join(record['warnings'])}"
        for record in records
        if record["warnings"]
    ]
    return {
        "version": SPEECH_NORMALIZATION_VERSION,
        "normalizer": "ai_morning_brief.writing.normalize_with_ledger",
        "records": records,
        "status": "passed" if not errors else "review",
        "errors": errors,
        "no_secrets_in_artifacts": True,
    }


def _summary_parts(summary: str | None) -> list[str]:
    pieces = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", _clean(summary)) if part.strip()]
    return pieces or [_clean(summary)] if _clean(summary) else []


def _draft_subject(title: str) -> str:
    """Pick a source token for the draft's required explicit subject field."""

    match = re.search(r"[A-Z][A-Za-z0-9.+#/-]{1,30}", title)
    if match:
        return match.group(0)
    return _clean(title)[:8]


def build_writing_request(editorial_input: Mapping[str, Any]) -> dict[str, Any]:
    """Create the Codex writer hand-off for a frozen source package."""

    body = {
        "version": WRITER_VERSION,
        "prompt_version": WRITER_PROMPT_VERSION,
        "input_sha256": str(editorial_input.get("input_sha256") or ""),
        "date": editorial_input.get("date"),
        "selection": dict(editorial_input.get("selection") or {}),
        "source_items": list(editorial_input.get("items") or []),
        "source_details": dict(editorial_input.get("source_details") or {}),
        "instructions": {
            "objective": "不要直接复述 AIHOT 原文；把每条已选资讯改写成清楚、流畅、有吸引力且严格可追溯的中文早报段落。先写 display_text，再由唯一标准化入口生成 spoken_text。",
            "overview": "每条提供 overview_text 和 overview_claim_ids；必须写明主体与具体动作、结果或事实，并至少引用一条 summary/detail claim，禁止直接拿 navigation_title 或品牌名充当概览。",
            "narration": "每条用具有稳定 beat_id 的 beat 完整解释具体事件和来源证据；不设置总字数、单 beat 字数、句数或视频时长上限。影响、行动、限制只在来源支持时写。beat 只绑定 claims，不为字幕宽度删减内容；字幕由下游按标点和画面宽度拆分。若有原文视觉，可按素材数量绑定一个或多个后置视觉 beat。",
            "cards": "卡片数量由有效 claim 和解释需要决定，不设固定上限；每张卡片有稳定 id、明确 subject 和独立信息职责。单页放不下时由渲染器自动分页，正文不得截断，metric 只在正文出现一次。",
            "grouping": "同一维度内，若多条资讯各自只有不超过两条独立支持 claim、正文短且不需要复杂时间线，可合并为一个 brief_group 场景；每组 2-4 条、每条恰好一张卡和一个 beat，共用一个顶部导航位。5-8 条必须拆成 3+2、3+3、4+3 或 4+4 等平衡分组，不能留下单条孤儿；维度头条/第一名和需要完整解释的故事保持 single。brief_group 必须填写 group_label、overview_items、card.source_item_id 和 beat.card_ids，且不展示评分或来源链接。",
            "speech": "spoken_text 由 ai_morning_brief.writing.normalize_with_ledger 唯一生成，不手写、不添加事实；显示数字去掉千位分隔符（例如 4，888→4888），朗读数字、tokens/s、GB、B/M/K、下划线代码和全大写缩写使用明确读法。",
            "grounding": "所有标题、卡片和 narration display 文案必须引用 exact source claims；不能补写来源未给出的数字、因果或预测，也不能逐字复制 AIHOT 标题或句子。",
        },
        "output_contract": {
            "plan_version": "5.0",
            "narration_beat_fields": ["beat_id", "type", "text", "claim_ids", "visual_asset_id"],
            "source_visual_policy": "X 原帖最多一个；普通网页首屏加至多一张正文图片，按视觉 beat 顺序展示",
            "story_fields": ["story_kind", "group_label", "subject", "navigation_title", "overview_text", "overview_claim_ids", "overview_items", "presentation_order"],
            "card_fields": ["id", "source_item_id", "subject", "role", "label", "headline", "body", "claim_ids"],
            "writer_metadata": {"skill": "ai-brief-editorial-writer", "version": WRITER_VERSION, "status": "approved"},
            "required_artifacts": ["editorial_draft.json", "editorial_plan.json", "editorial_plan_final.json", "editorial_quality_report.json", "pronunciation_ledger.json"],
        },
    }
    body["request_sha256"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return body


def write_writing_request(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_editorial_draft(editorial_input: Mapping[str, Any]) -> dict[str, Any]:
    """Build a grounded, deterministic starter draft for the Codex writer.

    This is intentionally labelled a *draft*: it gives the writing skill a
    complete, source-linked scaffold even when no model call is available. It
    is never silently promoted to a public edition without the editorial-plan
    validation gate.
    """

    stories: list[dict[str, Any]] = []
    for index, item in enumerate(editorial_input.get("items") or [], 1):
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or "")
        title = _clean(item.get("title"))
        summary = _clean(item.get("summary"))
        parts = _summary_parts(summary)
        first = parts[0] if parts else title
        second = parts[1] if len(parts) > 1 else first
        claims = [
            {"id": f"draft-{index}-title", "source_item_id": item_id, "source_field": "title", "source_text": title},
        ]
        if first:
            claims.append({"id": f"draft-{index}-summary-1", "source_item_id": item_id, "source_field": "summary", "source_text": first})
        if second and second != first:
            claims.append({"id": f"draft-{index}-summary-2", "source_item_id": item_id, "source_field": "summary", "source_text": second})
        fact = first or title
        subject = _draft_subject(title)
        overview = first or title
        beats = [
            {"beat_id": f"story-{index:02d}-beat-01", "type": "hook", "text": title, "claim_ids": [claims[0]["id"]]},
            {"beat_id": f"story-{index:02d}-beat-02", "type": "fact", "text": fact, "claim_ids": [claims[1]["id"] if len(claims) > 1 else claims[0]["id"]]},
        ]
        cards: list[dict[str, Any]] = [
            {"id": f"story-{index:02d}-card-01", "subject": subject, "role": "lead", "label": "核心变化", "headline": title[:18], "body": title, "span": 3, "claim_ids": [claims[0]["id"]]},
            {"id": f"story-{index:02d}-card-02", "subject": subject, "role": "evidence", "label": "已披露细节", "headline": "来源信息", "body": fact, "span": 3, "claim_ids": [claims[1]["id"] if len(claims) > 1 else claims[0]["id"]]},
        ]
        if len(parts) > 1:
            cards.append({"id": f"story-{index:02d}-card-03", "subject": subject, "role": "impact", "label": "编辑提示", "headline": "影响与关注点", "body": second, "span": 2, "claim_ids": [claims[-1]["id"]]})
        # Do not treat a bare model-version fragment such as ``3.8`` in
        # ``Qwen3.8`` as a standalone metric.  Only expose a metric hint when
        # the source attaches an explicit unit/date marker; the renderer then
        # highlights that exact phrase once in the body.
        numeric_match = re.search(
            r"(?<![A-Za-z])\d[\d,.，]*(?:\s*(?:tokens?/s|token|GB|GiB|MB|MiB|TB|TiB|[BMK]|%|倍|分|页|美元|亿|万|月|日|年))",
            summary,
            flags=re.IGNORECASE,
        )
        if numeric_match:
            metric = numeric_match.group(0).strip()
            for card in cards:
                if metric in str(card.get("body")) and metric not in str(card.get("headline") or "") and metric not in str(card.get("label") or ""):
                    card["metric"] = metric
                    break
        body_lengths = [len(str(card.get("body") or "")) for card in cards]
        density = "dense" if len(cards) >= 3 and sum(body_lengths) >= 100 else "regular"
        stories.append({
            "event_key": f"{item_id}:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:10]}",
            "presentation_order": index,
            "story_kind": "single",
            "source_item_ids": [item_id],
            "category": str(item.get("category") or "other"),
            "subject": subject,
            "navigation_title": f"{subject}：{title[:18]}",
            "overview_text": overview,
            "overview_claim_ids": [claims[1]["id"] if len(claims) > 1 else claims[0]["id"]],
            "title": title,
            "title_claim_ids": [claims[0]["id"]],
            "narration": {"beats": beats},
            "claims": claims,
            "layout": {"type": "impact-path" if len(cards) >= 3 else "stack", "density": density},
            "cards": cards,
        })
    return {
        "version": "5.0",
        "prompt_version": "codex-editorial-v5.1",
        "input_sha256": str(editorial_input.get("input_sha256") or ""),
        "writer": {"skill": "ai-brief-editorial-writer", "version": WRITER_VERSION, "status": "draft", "source": "deterministic-scaffold"},
        "stories": stories,
    }


def finalize_editorial_plan(plan: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach spoken narration variants and return the pronunciation ledger."""

    finalized = copy.deepcopy(dict(plan))
    plan_version = str(plan.get("version") or "")
    finalized["version"] = plan_version if plan_version in {"5.0", "4.0"} else "3.0"
    finalized["prompt_version"] = {
        "5.0": "codex-editorial-v5.1",
        "4.0": "codex-editorial-v4",
    }.get(finalized["version"], "codex-editorial-v3")
    writer = finalized.get("writer")
    if isinstance(writer, Mapping) and writer:
        writer_status = str(writer.get("status") or "")
        finalized["writer"] = {
            **dict(writer),
            "skill": "ai-brief-editorial-writer",
            "version": WRITER_VERSION,
            "status": "finalized" if writer_status in {"approved", "finalized"} else writer_status,
        }
    else:
        finalized["writer"] = {}
    finalized["speech"] = {"version": SPEECH_NORMALIZATION_VERSION, "provider_default": "azure", "canonical_text": "spoken_text"}
    for story in finalized.get("stories", []):
        if not isinstance(story, dict):
            continue
        narration = story.get("narration")
        if not isinstance(narration, dict):
            continue
        beats = narration.get("beats")
        if not isinstance(beats, list):
            continue
        caption_units: list[dict[str, Any]] = []
        for beat_index, beat in enumerate(beats, 1):
            if not isinstance(beat, dict):
                continue
            display = _clean(beat.get("text"))
            beat_id = str(beat.get("beat_id") or f"beat-{beat_index:02d}")
            beat["beat_id"] = beat_id
            beat_result = normalize_with_ledger(display)
            beat["text"] = beat_result.display_text
            beat["spoken_text"] = beat_result.spoken_text
            for unit_index, unit_text in enumerate(split_caption_units(beat_result.display_text), 1):
                # ``split_caption_units`` deliberately keeps a separator at
                # the edge where a long line is split.  Normalizing each unit
                # through ``_clean`` would strip that separator, causing the
                # concatenated narration to turn e.g. ``Mythos 5.1`` into
                # ``Mythos5.1`` and making whole-segment pronunciation
                # validation disagree with the authored text.
                leading_space = " " if unit_text[:1].isspace() else ""
                trailing_space = " " if unit_text[-1:].isspace() else ""
                result = normalize_with_ledger(unit_text.strip())
                unit_display = f"{leading_space}{result.display_text}{trailing_space}"
                unit_spoken = f"{leading_space}{result.spoken_text}{trailing_space}"
                caption_units.append({
                    "unit_id": f"{beat_id}-caption-{unit_index:02d}",
                    "beat_id": beat_id,
                    "beat_type": str(beat.get("type") or ""),
                    "display_text": unit_display,
                    "spoken_text": unit_spoken,
                    "claim_ids": [str(value) for value in beat.get("claim_ids", []) if value],
                    "card_ids": [str(value) for value in beat.get("card_ids", []) if value],
                    "visual_asset_id": str(beat.get("visual_asset_id") or ""),
                })
        narration["caption_units"] = caption_units
        narration["display_text"] = "".join(unit["display_text"] for unit in caption_units)
        narration["spoken_text"] = "".join(unit["spoken_text"] for unit in caption_units)
    # The plan-level ledger is generated again after finalization by the script
    # builder; this small preview helps the writer review its own edits.
    preview_segments = [
        {"id": f"story-{index:02d}", "display_text": story.get("narration", {}).get("display_text", ""), "spoken_text": story.get("narration", {}).get("spoken_text", "")}
        for index, story in enumerate(finalized.get("stories", []), 1)
        if isinstance(story, Mapping)
    ]
    return finalized, build_pronunciation_ledger({"segments": preview_segments})


def validate_spoken_text(script: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for segment in script.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        segment_id = str(segment.get("id") or "")
        display = str(segment.get("display_text") or segment.get("broadcast_text") or "")
        spoken = str(segment.get("spoken_text") or segment.get("broadcast_text") or "")
        if not spoken.strip():
            errors.append(f"{segment_id} has empty spoken_text")
            continue
        normalized = normalize_with_ledger(display)
        if spoken != normalized.spoken_text:
            errors.append(f"{segment_id} spoken_text is not normalized from display_text")
        if re.search(r"(?<![A-Za-z0-9])[A-Za-z0-9]+[/_][A-Za-z0-9]", spoken):
            errors.append(f"{segment_id} spoken_text retains a slash or underscore code")
    return errors


def source_number_tokens(text: str) -> tuple[str, ...]:
    """Expose numeric tokens for tests and provider benchmark reports."""

    return tuple(_NUMBER_RE.findall(str(text or "")))


def load_writing_request(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WritingError(f"could not read writing request: {path}") from exc
    if not isinstance(value, dict):
        raise WritingError("writing request must be a JSON object")
    return value
