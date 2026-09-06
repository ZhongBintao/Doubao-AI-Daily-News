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
# New editions select from four independent AIHOT dimensions.  The broader
# legacy order above remains for old fixtures and historical plans.
EDITORIAL_DIMENSIONS = ("ai-models", "ai-products", "industry", "paper")
EDITORIAL_DIMENSION_LABELS = {
    "ai-models": "模型",
    "ai-products": "产品",
    "industry": "行业",
    "paper": "论文",
}
DEFAULT_SELECTION_CANDIDATE_PAGE_SIZE = 50
DEFAULT_SELECTION_SOFT_MIN = 6
DEFAULT_SELECTION_MAX_ITEMS = 8
DEFAULT_SELECTION_HEAD_SHARE = 0.5
DEFAULT_BRIEF_GROUP_MAX_CARDS = 4
DEFAULT_VOICE = "zh-CN-Xiaochen:DragonHDLatestNeural"
DEFAULT_LOCALE = "zh-CN"
DEFAULT_RATE = "-5%"
DEFAULT_REGION = "southeastasia"
# DragonHD temperature trades deterministic repetition for a little more
# expressive variation. Keep this as one template-level setting so the SSML,
# provider adapter, and audit manifest cannot drift apart.
DEFAULT_AZURE_TTS_TEMPERATURE = 0.7

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
