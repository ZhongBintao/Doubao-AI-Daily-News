from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "outputs"
DEFAULT_OPENMONTAGE_ROOT = REPO_ROOT / "OpenMontage"
DEFAULT_AIHOT_URL = "https://aihot.virxact.com/api/v1/items"
DEFAULT_SOURCE_CONTRACT = "KKKKhazix/khazix-skills@7a5c4934be4106ac740ffdb95280bb81b3f4b83c"
DEFAULT_AIHOT_SKILL_VERSION = "1.5.4"
DEFAULT_SHOW_NAME = "AI每日早报"
DEFAULT_SHOW_NAME_EN = "AI Daily News"

# The visual taxonomy follows the supplied editorial reference while keeping
# the upstream AIHOT category slugs stable in the factual data model.
CATEGORY_LABELS = {
    "ai-models": "模型发布",
    "tip": "开发生态",
    "ai-products": "产品应用",
    "paper": "技术与洞察",
    "industry": "行业动态",
    "other": "前瞻与传闻",
}
CATEGORY_ORDER = tuple(CATEGORY_LABELS)
# New editions select from every known AIHOT dimension. ``tip`` used to be
# omitted here, which made a valid sparse day look empty when all fresh items
# belonged to that category. Unknown categories are appended dynamically by
# the pipeline instead of being silently dropped.
EDITORIAL_DIMENSIONS = CATEGORY_ORDER
EDITORIAL_DIMENSION_LABELS = {
    "ai-models": "模型",
    "tip": "开发生态",
    "ai-products": "产品",
    "industry": "行业",
    "paper": "论文",
    "other": "其他",
}
DEFAULT_SELECTION_CANDIDATE_PAGE_SIZE = 50
DEFAULT_SELECTION_SOFT_MIN = 6
DEFAULT_SELECTION_MAX_ITEMS = 8
DEFAULT_SELECTION_HEAD_SHARE = 0.5
DEFAULT_SELECTION_MINIMUM_ITEMS = 1
DEFAULT_SELECTION_FALLBACK_WINDOW = "7d"
DEFAULT_SELECTION_FALLBACK_MAX_ITEMS = 3
DEFAULT_BRIEF_GROUP_MAX_CARDS = 4
DEFAULT_VOICE = "zh-CN-Xiaochen:DragonHDLatestNeural"
DEFAULT_LOCALE = "zh-CN"
DEFAULT_RATE = "-5%"
DEFAULT_REGION = "southeastasia"
# DragonHD temperature trades deterministic repetition for a little more
# expressive variation. Keep this as one template-level setting so the SSML,
# provider adapter, and audit manifest cannot drift apart.
DEFAULT_AZURE_TTS_TEMPERATURE = 0.7

# Doubao in-conversation speech synthesis (cloud-computer native production
# voice).  Two modes are supported and share the same pipeline:
#
#   text-to-audio             ``text_to_audio_plus`` with the natural-language
#                             voice profile below.  No reference audio needed.
#   voice-clone    (default on this branch) ``audio_to_audio_plus`` with a
#                             reference clip.
#
# This branch is the voice-clone rollback pointer: it is byte-identical to
# feat/tts-text-to-audio apart from ``DEFAULT_TTS_MODE`` below.  Both modes
# remain fully supported on either branch (``--tts-mode`` overrides).
#
# The voice profile is the single source of truth for the narration voice and
# its tempo: it is embedded in ``tts_manifest.json``, the agent handoff and the
# ``ai-brief-doubao-voice`` skill, so changing the voice never requires editing
# three places by hand.
DOUBAO_TTS_VOICE_PROFILE = (
    "年轻女性，声音温柔甜美，语速比正常快约 25%，咬字清晰有节奏感，"
    "情绪明亮自然，像晨间新闻女主播，无背景音无杂音，纯人声"
)
DEFAULT_TTS_MODE = "voice-clone"
DOUBAO_PROVIDER = "doubao"
DOUBAO_ALIGNMENT_PROVIDER = "doubao-proportional"

# Voice cloning stays fully supported as an explicit fallback
# (``--tts-mode voice-clone`` / ``--speech-provider doubao-voice-clone``).
# The reference clip still ships with the repo so a cloud-computer reset only
# needs a git pull to restore it.  Override via DOUBAO_VOICE_CLONE_REFERENCE.
DEFAULT_VOICE_CLONE_REFERENCE_AUDIO = REPO_ROOT / "example-audio.mp3"
DOUBAO_VOICE_CLONE_PROVIDER = "doubao-voice-clone"
DOUBAO_VOICE_CLONE_ALIGNMENT = "doubao-voice-clone-proportional"

# Last-resort tempo correction, OFF by default (1.0).
# The voice profile already asks the model for a faster delivery.  If a given
# engine ignores that instruction, setting AI_BRIEF_SPEECH_TEMPO=1.25 speeds up
# each normalized narration block with ffmpeg's pitch-preserving ``atempo``;
# every downstream duration, caption and music boundary follows the new
# length automatically because durations are measured from the final audio.
DEFAULT_SPEECH_TEMPO = 1.0
SPEECH_TEMPO_MIN = 0.5
SPEECH_TEMPO_MAX = 2.0


def speech_tempo() -> float:
    """Return the configured narration tempo multiplier (1.0 = untouched)."""

    raw = os.environ.get("AI_BRIEF_SPEECH_TEMPO")
    if not raw:
        return DEFAULT_SPEECH_TEMPO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SPEECH_TEMPO
    if not (SPEECH_TEMPO_MIN <= value <= SPEECH_TEMPO_MAX):
        return DEFAULT_SPEECH_TEMPO
    return value

# Optional source-visual mode is deliberately off by default. These values are
# configuration points rather than editorial content, so production can tune
# pacing without changing the frozen source or card schema.
DEFAULT_X_SCREENSHOT_MODE = "off"
X_SCREENSHOT_PAGE_DURATION_SECONDS = 4.5
X_SCREENSHOT_LEAD_SECONDS = 0.5
# Generalized source-visual pacing. Legacy X names above remain for manifest
# compatibility with existing dated runs.
# Every story with a grounded, sufficiently sharp source asset may use one
# visual.  Selection is no longer capped per edition.
SOURCE_VISUAL_DURATION_SECONDS = 4.5
SOURCE_VISUAL_TRANSITION_SECONDS = 0.35
SOURCE_VISUAL_MIN_CARD_READ_SECONDS = 8.0
SOURCE_VISUAL_CARD_CHARS_PER_SECOND = 10.0
# Overview pages are intentionally paced as a fixed visual index.  Keep this
# value shared by script planning and HTML materialization so the page switch
# cannot drift from the editorial contract.
OVERVIEW_PAGE_DURATION_SECONDS = 5.0


def editorial_dimensions_for_categories(categories: object) -> tuple[str, ...]:
    """Return known categories in editorial order plus future AIHOT slugs."""

    present = {
        str(value or "other").strip() or "other"
        for value in (categories if isinstance(categories, (list, tuple, set, frozenset)) else ())
    }
    if not present:
        return EDITORIAL_DIMENSIONS
    known = [value for value in CATEGORY_ORDER if value in present]
    unknown = sorted(present.difference(CATEGORY_ORDER))
    return tuple(known + unknown)


def env_path() -> Path:
    return Path(os.environ.get("AI_MORNING_BRIEF_ENV", REPO_ROOT / ".env")).expanduser()


def load_allowed_env(path: Path | None = None) -> dict[str, str]:
    """Load only provider variables needed by the local renderer/benchmarks.

    The file is parsed directly instead of being sourced.  Values are returned
    to the in-process renderer and are never included in reports or logs.
    """

    path = path or env_path()
    allowed = {
        "AZURE_SPEECH_KEY",
        "AZURE_SPEECH_REGION",
        "AZURE_TTS_ENDPOINT",
        "AZURE_SPEECH_ENDPOINT",
        "GEMINI_API_KEY",
        "GOOGLE_AI_STUDIO_API_KEY",
        "GEMINI_TTS_MODEL",
        "GEMINI_TTS_VOICE",
    }
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in allowed:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                values[key] = value
    # Google AI Studio names its key independently from the Gemini SDK's
    # conventional variable. Keep the project-facing name intact, while
    # exposing a process-local alias so the provider adapter can use the same
    # client contract in both shell and .env runs.
    if values.get("GOOGLE_AI_STUDIO_API_KEY") and not values.get("GEMINI_API_KEY"):
        values["GEMINI_API_KEY"] = values["GOOGLE_AI_STUDIO_API_KEY"]
    return values
