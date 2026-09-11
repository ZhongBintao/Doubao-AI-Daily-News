"""Text normalization and fuzzy matching utilities for the speech QA gate.

The ASR quality gate compares what the voice model actually said (a local
whisper transcript) against the authored narration text.  The two strings
never match literally: whisper transcribes ``260`` as ``二百六十``, spells
acronyms with spaces (``G P U``), and adds punctuation.  This module provides
the canonical, dependency-free normalization that makes those variants
comparable, plus the ordered fuzzy matcher that maps caption units onto
transcript spans so real word timestamps can be attached to display text.

Both ``speech_qa`` (verdicts) and ``media`` (ASR-timed captions) import from
here; it deliberately has no intra-package dependencies.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Mapping, Sequence


_ZH_DIGIT_VALUES = {
    "零": 0, "〇": 0,
    "一": 1, "二": 2, "两": 2,
    "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_ZH_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_ZH_GROUP_UNITS = {"万": 10 ** 4, "亿": 10 ** 8, "兆": 10 ** 12}
_ZH_NUMBER_RUN_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿兆点]+")

# Anything not preserved by the matcher is treated as noise: whitespace and
# punctuation in both scripts, CJK fullwidth variants, quotes and dashes.
_KEEP_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _zh_run_to_arabic(run: str) -> str | None:
    """Convert one continuous Chinese numeral run to an Arabic numeral string.

    Handles both positional readings (``二千零二十`` -> 2020) and digit-list
    readings (``二零二零`` -> 2020), plain tens (``十五`` -> 15), grouped
    magnitudes (``五千五百二十亿``) and decimal ``点`` (``三点五`` -> 3.5).
    Returns None when the run is not a well-formed numeral.
    """

    if not run:
        return None
    if "点" in run:
        whole, _, frac = run.partition("点")
        whole_value = _zh_int(whole) if whole else 0
        if whole_value is None:
            return None
        frac_digits = "".join(str(_ZH_DIGIT_VALUES[char]) for char in frac if char in _ZH_DIGIT_VALUES)
        if not frac_digits:
            return None
        return f"{whole_value}.{frac_digits}"
    if any(char in _ZH_SMALL_UNITS or char in _ZH_GROUP_UNITS for char in run):
        value = _zh_int(run)
        return str(value) if value is not None else None
    # Pure digit-list reading ("二零二五").  Concatenate digit glyphs.
    digits = "".join(str(_ZH_DIGIT_VALUES[char]) for char in run if char in _ZH_DIGIT_VALUES)
    return digits or None


def _zh_int(run: str) -> int | None:
    total = 0
    section = 0
    number = 0
    seen_digit = False
    for char in run:
        if char in _ZH_DIGIT_VALUES:
            number = _ZH_DIGIT_VALUES[char]
            seen_digit = True
        elif char in _ZH_SMALL_UNITS:
            if number == 0 and not seen_digit:
                number = 1  # 「十五」starts with an implicit 一
            section += number * _ZH_SMALL_UNITS[char]
            number = 0
        elif char in _ZH_GROUP_UNITS:
            magnitude = section + number
            section = (magnitude if magnitude else 1) * _ZH_GROUP_UNITS[char]
            total += section
            section = 0
            number = 0
        else:
            return None
    if not seen_digit and not total and not section:
        return None
    return total + section + number


def normalize_for_match(text: str) -> str:
    """Canonical comparison form: NFKC, lowercased, numerals in Arabic,
    whitespace/punctuation stripped.

    ``GPU``、``G P U``、``基 皮 优`` (ASCII keep) collapse to ``gpu``;
    ``RSA-260``、``RSA 260``、``阿二六零``-style variants collapse on the
    alphanumeric run; ``二百六十`` and ``260`` both become ``260``.
    """

    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = _ZH_NUMBER_RUN_RE.sub(lambda match: _convert_zh_run(match.group(0)), value)
    return _KEEP_RE.sub("", value)


def _convert_zh_run(run: str) -> str:
    converted = _zh_run_to_arabic(run)
    return converted if converted is not None else run


def extract_key_tokens(text: str) -> list[str]:
    """Extract the tokens whose correct reading is a fact-safety requirement:
    Arabic numerals (with optional unit suffix, ex ``552B``) and ASCII words
    of two or more characters."""

    value = unicodedata.normalize("NFKC", str(text or ""))
    tokens: list[str] = []
    for match in re.finditer(r"[0-9]+(?:[.,][0-9]+)?[A-Za-z]*|[A-Za-z][A-Za-z0-9.+#/-]+", value):
        token = match.group(0)
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def find_repetition(normalized: str, *, minimum_length: int = 4, maximum_length: int = 24) -> tuple[int, int] | None:
    """Locate the first immediately-repeated span (immediate stutter).

    Returns ``(start, length)`` of the repeated fragment inside ``normalized``
    such that ``normalized[start:start+length] == normalized[start+length:start+2*length]``,
    or None.  Normal Chinese prose does not contain adjacent 4+ character
    repeats, while a cloning-model stutter reliably does.
    """

    text = str(normalized or "")
    limit = min(maximum_length, len(text) // 2)
    for length in range(minimum_length, limit + 1):
        for start in range(0, len(text) - 2 * length + 1):
            if text[start:start + length] == text[start + length:start + 2 * length]:
                return start, length
    return None


def compare_texts(expected: str, transcript: str, *, minimum_similarity: float = 0.72, extra_expected_tokens: Sequence[str] = ()) -> dict:
    """Judge one transcript against its expected narration text.

    ``extra_expected_tokens`` lets a colloquial-override attempt still be held
    to the facts of the on-screen display text: tokens listed there must also
    be audible even though the spoken wording was paraphrased.
    The verdict keys match the retry policy in ``speech_qa``:
    ``pass`` / ``low_similarity`` / ``missing_tokens`` (facts read wrong or
    skipped) / ``repetition`` (stutter) / ``extra_content`` (hallucinated
    additions) / ``empty_transcript`` (nothing intelligible spoken).
    """

    expected_normalized = normalize_for_match(expected)
    transcript_normalized = normalize_for_match(transcript)
    report: dict = {
        "expected_normalized": expected_normalized,
        "transcript_normalized": transcript_normalized,
        "similarity": 0.0,
        "missing_tokens": [],
        "repetition": None,
        "extra_content": False,
        "verdict": "pass",
        "reason": "",
    }
    if not expected_normalized:
        report["verdict"] = "pass"
        report["reason"] = "empty expected text"
        return report
    if not transcript_normalized:
        report["verdict"] = "empty_transcript"
        report["reason"] = "ASR produced no intelligible content"
        return report
    ratio = difflib.SequenceMatcher(None, expected_normalized, transcript_normalized).ratio()
    report["similarity"] = round(ratio, 4)
    # Fact tokens: every numeral and multi-char ASCII word in the display text
    # must survive normalization somewhere in the transcript.  A bare numeric
    # fallback is accepted so unit-spelling differences (552B vs 五千五百二十亿
    # vs 552000000000) do not fail an otherwise faithful reading.
    missing: list[str] = []
    for token in list(extract_key_tokens(expected)) + [str(token) for token in extra_expected_tokens]:
        token_key = normalize_for_match(token)
        if token_key and token_key in transcript_normalized:
            continue
        digits = re.sub(r"[^0-9]", "", token)
        if digits and digits in transcript_normalized:
            continue
        if token not in missing:
            missing.append(token)
    report["missing_tokens"] = missing
    repetition = find_repetition(transcript_normalized)
    report["repetition"] = list(repetition) if repetition else None
    extra_content = len(transcript_normalized) > max(len(expected_normalized) * 1.35, len(expected_normalized) + 12)
    report["extra_content"] = extra_content
    if repetition is not None:
        report["verdict"] = "repetition"
        report["reason"] = f"repeated span at {repetition[0]} (len {repetition[1]})"
    elif missing:
        report["verdict"] = "missing_tokens"
        report["reason"] = "expected tokens not heard: " + ", ".join(missing[:8])
    elif extra_content:
        # More specific than a bare similarity miss: the voice model added
        # content, which is what a caller must fix first.
        report["verdict"] = "extra_content"
        report["reason"] = "transcript much longer than the narration text"
    elif ratio < minimum_similarity:
        report["verdict"] = "low_similarity"
        report["reason"] = f"similarity {ratio:.3f} below {minimum_similarity:.2f}"
    else:
        report["verdict"] = "pass"
        report["reason"] = "reading matches the authored text"
    return report


def locate_words_in_normalized(words: Sequence[Mapping[str, Any]], normalized_transcript: str) -> list[tuple[int, int]]:
    """Map each transcript word onto its span in the normalized transcript.

    ``words`` are dicts with a ``word`` (or ``text``) entry.  Returns a list
    parallel to ``words``: ``(start, end)`` key-space span, or ``(-1, -1)``
    when the word carries no keepable characters (punctuation).  Word order
    follows the transcript, so a monotonic forward scan is exact even for
    repeated words.
    """

    spans: list[tuple[int, int]] = []
    cursor = 0
    for word in words:
        raw = ""
        if isinstance(word, Mapping):
            raw = str(word.get("word") or word.get("text") or "")
        key = normalize_for_match(raw)
        if not key:
            spans.append((-1, -1))
            continue
        position = normalized_transcript.find(key, cursor)
        if position < 0:
            position = normalized_transcript.find(key)
        if position < 0:
            spans.append((-1, -1))
            continue
        spans.append((position, position + len(key)))
        cursor = position + len(key)
    return spans


def match_units_in_transcript(unit_texts: Sequence[str], normalized_transcript: str) -> list[tuple[int, int] | None]:
    """Find each caption unit's ordered span inside the normalized transcript.

    Units are matched in order from a monotonic cursor.  Exact substring
    matching is tried first; when it fails (ASR misheard a character), a
    bounded fuzzy search picks the best local alignment whose similarity is
    still meaningful.  Unmatched units return None and the caller falls back
    to interpolation between neighbouring matches.
    """

    spans: list[tuple[int, int] | None] = []
    cursor = 0
    for raw in unit_texts:
        key = normalize_for_match(raw)
        if not key:
            spans.append(None)
            continue
        exact = normalized_transcript.find(key, cursor)
        if exact >= 0:
            spans.append((exact, exact + len(key)))
            cursor = exact + len(key)
            continue
        best: tuple[int, int, float] | None = None
        window = len(key) * 2
        search_start = max(0, cursor - max(4, len(key)))
        search_end = min(len(normalized_transcript), cursor + window)
        for start in range(search_start, max(search_start + 1, search_end - max(1, len(key) // 2))):
            candidate = normalized_transcript[start:start + len(key)]
            if not candidate:
                break
            ratio = difflib.SequenceMatcher(None, key, candidate).ratio()
            if ratio < 0.6:
                continue
            if best is None or ratio > best[2]:
                best = (start, start + len(candidate), ratio)
        if best is not None:
            spans.append((best[0], best[1]))
            cursor = best[1]
            continue
        spans.append(None)
    return spans
