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


def fact_tokens(text: str) -> tuple[list[str], list[str]]:
    """Split a text's key tokens into (numeric, ascii) fact classes.

    ``numeric`` are tokens a listener must hear to keep the facts: Arabic
    numerals with optional unit suffixes (``552B``, ``2020``) plus the bare
    digit substrings of alphanumeric codes (``RSA-260`` -> ``260``).
    ``ascii`` are alphabetic tokens (``GPU``, ``DeepSeek``) whose exact ASR
    transcription is unreliable — a missing one is recorded as a warning,
    never a failure.
    """

    numeric: list[str] = []
    ascii_tokens: list[str] = []
    for token in extract_key_tokens(text):
        if token[0].isdigit():
            if token not in numeric:
                numeric.append(token)
        else:
            if token not in ascii_tokens:
                ascii_tokens.append(token)
            digits = re.sub(r"[^0-9]", "", token)
            if digits and digits not in numeric:
                numeric.append(digits)
    return numeric, ascii_tokens


def compare_texts(expected: str, transcript: str, *, minimum_similarity: float = 0.72, extra_expected_tokens: Sequence[str] = ()) -> dict:
    """Judge one transcript against its expected narration text.

    ``extra_expected_tokens`` lets a colloquial-override attempt still be held
    to the facts of the on-screen display text: tokens listed there must also
    be audible even though the spoken wording was paraphrased.

    Verdicts are two-tier by design (cloud feedback 2026-09: whisper-small
    mishears mixed-language technical terms and transcribed garbage as
    repetition, which used to fail otherwise-correct audio):

    * hard failures — only high-confidence structural faults of the reading
      itself: ``empty_transcript``, ``missing_numeric_tokens`` (digits from
      the display text never heard), ``repetition`` (the repeated span is
      real authored text, i.e. a genuine stutter), ``major_omission`` (half
      the narration is missing) and ``low_similarity`` below a hard floor.
    * warnings — anything that may equally be the ASR's own weakness:
      unmatched English terms, whisper hallucination-style repetition that
      does not exist in the authored text, extra tail content, moderate
      similarity misses.  Verdict stays ``pass``; the warnings are recorded
      in the QA report for human review.
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
        "warnings": [],
        "verdict": "pass",
        "reason": "",
    }
    if not expected_normalized:
        report["reason"] = "empty expected text"
        return report
    if not transcript_normalized:
        report["verdict"] = "empty_transcript"
        report["reason"] = "ASR produced no intelligible content"
        return report
    ratio = difflib.SequenceMatcher(None, expected_normalized, transcript_normalized).ratio()
    report["similarity"] = round(ratio, 4)

    # Fact tokens split into numeric (hard) and ascii (soft) classes.
    numeric_tokens, ascii_tokens = fact_tokens(expected)
    for token in extra_expected_tokens:
        token_numeric, token_ascii = fact_tokens(str(token))
        numeric_tokens.extend(t for t in token_numeric if t not in numeric_tokens)
        ascii_tokens.extend(t for t in token_ascii if t not in ascii_tokens)

    def heard(token: str) -> bool:
        key = normalize_for_match(token)
        if key and key in transcript_normalized:
            return True
        digits = re.sub(r"[^0-9]", "", token)
        return bool(digits) and digits in transcript_normalized

    warnings: list[str] = report["warnings"]

    # 1. Numeric facts: the one class a degraded ASR still gets right, and
    #    the one thing a skipped or misread number must never survive.
    missing_numeric = [token for token in numeric_tokens if not heard(token)]
    report["missing_tokens"] = missing_numeric
    if missing_numeric:
        report["verdict"] = "missing_numeric_tokens"
        report["reason"] = "numeric facts not heard: " + ", ".join(missing_numeric[:8])
        return report

    # 2. Repetition: a genuine stutter repeats authored text; a repeated span
    #    that exists nowhere in the authored text is whisper hallucination.
    repetition = find_repetition(transcript_normalized)
    report["repetition"] = list(repetition) if repetition else None
    if repetition is not None:
        repeated_span = transcript_normalized[repetition[0]:repetition[0] + repetition[1]]
        if repeated_span in expected_normalized:
            report["verdict"] = "repetition"
            report["reason"] = f"authored text repeated at {repetition[0]} (len {repetition[1]})"
            return report
        warnings.append(f"asr_hallucination_repetition: repeated span not present in narration")

    # 3. English terms: small ASR models routinely miss mixed-language
    #    technical vocabulary.  Never fail on these; record what was lost.
    unmatched_ascii = [token for token in ascii_tokens if not heard(token)]
    degraded_english = bool(ascii_tokens) and len(unmatched_ascii) == len(ascii_tokens)
    if degraded_english:
        warnings.append("asr_degraded_english: no English term was recognized (" + ", ".join(ascii_tokens[:6]) + ")")
    elif unmatched_ascii:
        warnings.append("ascii_token_unmatched: " + ", ".join(unmatched_ascii[:6]))

    # 4. Major omission: over half the narration missing is a structural
    #    fault a degraded ASR does not fake (it garbles, it does not shorten
    #    faithful Chinese reading by half).
    if len(transcript_normalized) < 0.45 * len(expected_normalized):
        report["verdict"] = "major_omission"
        report["reason"] = f"transcript covers less than half the narration ({len(transcript_normalized)}/{len(expected_normalized)} chars)"
        return report

    # 5. Similarity: hard floor for wholesale mismatch, warning for moderate.
    #    When the ASR itself failed on every English term, the overall ratio
    #    is depressed by exactly those missing tokens — judge the hard floor
    #    on the Chinese skeleton instead, with the English tokens removed
    #    from the expected side.
    floor_ratio = ratio
    floor_basis = "full text"
    if degraded_english:
        chinese_expected = expected_normalized
        for token in ascii_tokens:
            chinese_expected = chinese_expected.replace(normalize_for_match(token), "")
        if chinese_expected:
            floor_ratio = difflib.SequenceMatcher(None, chinese_expected, transcript_normalized).ratio()
            floor_basis = "chinese skeleton"
    report["similarity"] = round(ratio, 4)
    report["chinese_skeleton_similarity"] = round(floor_ratio, 4) if degraded_english else None
    extra_content = len(transcript_normalized) > max(len(expected_normalized) * 1.35, len(expected_normalized) + 12)
    report["extra_content"] = extra_content
    if floor_ratio < 0.45:
        report["verdict"] = "low_similarity"
        report["reason"] = f"similarity {floor_ratio:.3f} ({floor_basis}) far below the hard floor 0.45"
        return report
    if ratio < minimum_similarity:
        warnings.append(f"low_similarity: {ratio:.3f} below target {minimum_similarity:.2f} (accepted)")
    if extra_content:
        warnings.append("extra_content: transcript longer than the narration text (possibly ASR tail hallucination)")

    if warnings:
        report["reason"] = "accepted with warnings: " + "; ".join(warnings[:3])
    else:
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
