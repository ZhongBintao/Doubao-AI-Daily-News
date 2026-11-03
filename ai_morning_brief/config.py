from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "outputs"
DEFAULT_OPENMONTAGE_ROOT = REPO_ROOT / "OpenMontage"
DEFAULT_AIHOT_URL = "https://aihot.news/api/v1/items"
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

# Doubao voice cloning (cloud-computer native production voice).
# The reference audio ships with the repo so a cloud-computer reset only
# needs a git pull to restore it.  Override via DOUBAO_VOICE_CLONE_REFERENCE.
DEFAULT_VOICE_CLONE_REFERENCE_AUDIO = REPO_ROOT / "example-audio.mp3"
DEFAULT_TTS_MODE = "voice-clone"
DOUBAO_VOICE_CLONE_PROVIDER = "doubao-voice-clone"
DOUBAO_VOICE_CLONE_ALIGNMENT = "doubao-voice-clone-proportional"

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


# ---------------------------------------------------------------------------
# v6 vertical edition (TikTok / 小红书). Every tunable of the new pipeline
# lives here so weights and budgets can change without touching logic.
# ---------------------------------------------------------------------------
V6_VERSION = "6.0"
AIHOT_API_BASE = "https://aihot.news/api/v1"
AIHOT_API_USER_AGENT = "aihot-api/2.0.0 (+https://github.com/ZhongBintao/Doubao-AI-Daily-News)"
# AIHOT daily reports cover Beijing 08:00 -> 08:00 and are published at 08:00.
AIHOT_DAILY_CUTOFF_HOUR = 8

# Heat score = AIHOT score + signal bonuses + category adjustment.
HEAT_DEFAULT_SCORE = 65
HEAT_DAILY_BONUS = 8
HEAT_DAILY_LEAD_BONUS = 12
HEAT_DAILY_FLASH_BONUS = 4
HEAT_HOT_RANK_BASE = 22          # bonus = max(0, base - step * rank)
HEAT_HOT_RANK_STEP = 2
HEAT_STALE_EVENT_PENALTY = 10    # hot event first reported long before the window
HEAT_STALE_EVENT_HOURS = 48
HEAT_HOT_SOURCE_BONUS = 2        # per reporting source, capped below
HEAT_HOT_SOURCE_CAP = 5
HEAT_CATEGORY_ADJUST = {
    "ai-models": 6, "ai-products": 3, "industry": 0, "paper": -2, "opinion": -8, "tip": -10, "other": -4,
}
HEAT_AIRED_PENALTY = 40          # an event aired in the recent window drops out unless it is still the top story
HEAT_HEAVY_THRESHOLD = 88        # "重磅"
HEAT_FLASH_THRESHOLD = 76        # good enough for a 快讯
AIRED_LOOKBACK_DAYS = 3
CANDIDATE_POOL_SIZE = 8          # what the writer model may choose from

# Rundown: 1 headline (2 when two heavy stories) + flashes, max 4 stories.
RUNDOWN_MAX_STORIES = 4
RUNDOWN_MAX_HEADLINES = 2
RUNDOWN_DEFAULT_FLASHES = 3
RUNDOWN_QUIET_FLASHES = 2

# Writing budgets (visible characters; ~4.7 chars/s spoken).
BUDGET_HOOK_MAX = 28
BUDGET_KEYFACT_MAX = 60
BUDGET_TITLE_MAX = 32
BUDGET_CARD_LABEL_MAX = 6
BUDGET_CARD_HEADLINE_MAX = 16
BUDGET_CARD_BODY_MAX = 48
BUDGET_SENTENCE_MAX = 42
BUDGET_HEADLINE_NARRATION_MAX = 170
BUDGET_FLASH_POINT_MAX = 56
BUDGET_FLASH_NARRATION_MAX = 70
BUDGET_TOTAL_NARRATION_MAX = 360
HEADLINE_CARDS_MIN = 2
HEADLINE_CARDS_MAX = 5
CARDS_PER_PAGE = 3

# Video
V6_WIDTH = 1080
V6_HEIGHT = 1920
V6_FPS = 30
V6_MAX_DURATION_SECONDS = 100.0
V6_HOOK_MAX_SECONDS = 4.0
PAUSE_AFTER_HOOK = 0.30
PAUSE_BETWEEN_SENTENCES = 0.14
PAUSE_BETWEEN_STORIES = 0.40
OUTRO_TEXT = "关注我，每天一分钟看懂 AI。"
OUTRO_MIN_SECONDS = 2.6

# Voice
TTS_PROVIDERS = ("doubao", "edge")
DEFAULT_TTS_PROVIDER = os.environ.get("AI_DAILY_TTS_PROVIDER", "doubao")
EDGE_TTS_VOICE = os.environ.get("EDGE_TTS_VOICE", "zh-CN-YunxiNeural")
EDGE_TTS_RATE = os.environ.get("EDGE_TTS_RATE", "+10%")

# Mix: music sits well under the voice and ducks further while speaking.
MIX_MUSIC_UNDER_VOICE_DB = 18.0
MIX_SIDECHAIN_RATIO = 4.0
MIX_TARGET_LUFS = -16.0
MIX_TRUE_PEAK = -1.5

# Category -> template accent class and label.
V6_CATEGORY_LABELS = {
    "ai-models": "模型发布", "ai-products": "产品动态", "industry": "行业动态",
    "paper": "论文研究", "tip": "技巧教程", "opinion": "观点", "other": "AI 动态",
}
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
FONT_FILENAME = "NotoSansSC-VF.ttf"
FONT_CACHE_DIR = REPO_ROOT / ".local" / "fonts"


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
