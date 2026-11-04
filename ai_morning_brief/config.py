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
CANDIDATE_POOL_SIZE = 12         # what the writer model may choose from

# Material pack: extra AIHOT-only coverage gathered for the strongest events.
# Only aihot.news endpoints are read; original article links are never opened.
ENRICH_CLUSTERS = 12             # events that get a keyword search for more AIHOT coverage
ENRICH_RELATED_EVENTS = 4        # events whose AIHOT "related stories" are fetched as background
ENRICH_RELATED_PER_EVENT = 2
SEARCH_LIMIT = 20
PACK_MAX_FACTS = 14              # fact paragraphs per event (summary, digest, per-source reports, coverage)
PACK_MAX_FACT_CHARS = 3600
PACK_MAX_BACKGROUND = 4
PACK_DEPTH_RICH = 700            # fact characters for a "rich" pack
PACK_DEPTH_MEDIUM = 300

# Rundown: the day's content decides the count. Heavy events with enough
# material become headlines (max 3), the rest above the flash line become
# flashes (max 5). There is no overall time cap (removed 2026-11-04).
RUNDOWN_MAX_HEADLINES = 3
RUNDOWN_MAX_FLASHES = 5
RUNDOWN_QUIET_FLASHES = 2
# Ranking bonus inside the thresholds: close in heat -> the event with more AIHOT material wins the slot.
RUNDOWN_DEPTH_BONUS = {"rich": 6, "medium": 3, "thin": 0}
SPEECH_CHARS_PER_SECOND = 5.5      # visible units/s (English letters count one each); edge-tts ~6.5, Doubao natural ~5.7

# Writing budgets (visible units: a Han character or one English letter/digit). Story budgets
# (narration min/max, card count) scale with the event's material depth.
BUDGET_HOOK_MAX = 24             # spoken within the 4.5 s hook gate
BUDGET_KEYFACT_MAX = 60
BUDGET_TITLE_MAX = 30
BUDGET_CARD_LABEL_MAX = 6
BUDGET_CARD_HEADLINE_MAX = 16
BUDGET_CARD_BODY_MAX = 60
BUDGET_SENTENCE_MAX = 42
BUDGET_FLASH_POINT_MAX = 60
BUDGET_FLASH_WHY_MAX = 60
STORY_BUDGETS = {
    # depth: headline narration (min, max), headline cards (min, max), flash narration (min, max), flash sentences (min, max)
    "rich": {"headline": (150, 300), "cards": (3, 6), "flash": (45, 110), "flash_sentences": (2, 3)},
    "medium": {"headline": (100, 220), "cards": (3, 5), "flash": (40, 100), "flash_sentences": (2, 3)},
    "thin": {"headline": (60, 140), "cards": (2, 4), "flash": (25, 80), "flash_sentences": (1, 3)},
}
BUDGET_MIN_SHARE_OF_MATERIAL = 0.4   # never demand more narration than this share of the material
# Card angle: a hidden multiple choice (icon + depth check); the on-screen card title is free, hook-style text.
CARD_ANGLES = ("发生了什么", "关键数字", "怎么做到的", "各方说法", "背景", "为什么重要", "对你的影响", "还没确定", "下一步")
CARD_DEPTH_ANGLES = ("各方说法", "背景", "为什么重要", "对你的影响")
CARD_LABELS, CARD_DEPTH_LABELS = CARD_ANGLES, CARD_DEPTH_ANGLES   # v6.1 names

# Hook-style titles (contract 6.2): pick a formula, write 3 candidates, keep the best.
# Curiosity, never deception: every fact is in the material and every suspense is paid off in the story.
TITLE_FORMULAS = {
    "反差": "同一件事里两个相反的事实：「营收一年涨 12 倍，却转亏了」",
    "数字冲击": "最惊人的一个数字放前面：「只改 0.016% 的权重，成绩翻 6 倍」",
    "提问悬念": "抛出观众想知道的问题，答案在本条里：「一年涨 12 倍，为什么还亏？」",
    "与你有关": "点出普通用户会感受到的变化：「12 月起，ChatGPT 和 Codex 要抢同一份额度」",
    "揭秘": "说有一个藏着的细节，答案在本条里：「招股书里藏着一句警告」",
    "对立": "两方说法或两种走向相反：「一边说已复现，一边说没验证」",
    "剧透一半": "先给一半，另一半在本条里：「Anthropic 要上市了，最吓人的不是营收」",
    "直接结论": "素材里没有反差或数字时，一句话说清：「马斯克亲口承认：Grok Bot 太烧钱了」",
}
SUSPENSE_FORMULAS = ("提问悬念", "揭秘", "剧透一半")   # these must name the card that pays them off
TITLE_CANDIDATES = 3
TITLE_BANNED = ("震惊", "炸裂", "颠覆", "史上最", "全网", "速看", "必看", "太可怕", "疯了", "刚刚", "重磅", "惊呆", "吓尿",
                "凉了", "封神", "绝了", "离谱", "爆了", "逆天", "吊打", "碾压", "王炸", "细思极恐", "万万没想到",
                "狂飙", "暴涨", "暴跌", "杀疯", "血洗", "秒杀", "慌了", "完爆", "天花板")
# Tone the user chose (2026-11-04, "A 档"): colloquial, a vivid verb or metaphor, a rhetorical question at most -
# punchy but every word still true to the material. Shown to the writer as before/after pairs.
TITLE_TONE_EXAMPLES = [
    {"before": "孩子用的 Google，明年起 AI 默认关闭", "after": "AI 和搜索绑死，Google 给学校出了道单选题"},
    {"before": "AMD 明年八成销售额，都押在一样东西上", "after": "AMD 放话：明年营收要涨八成"},
    {"before": "ChatGPT 的搜索量，已经是 Google 的两成", "after": "Google 的五分之一：ChatGPT 搜索量追上来了"},
    {"before": "你用的 AI，真是它说的那个模型吗？", "after": "AI 也会被偷换？Anthropic 做了个“验货”工具"},
    {"before": "Mistral Large 4 开放权重，还公开了研发全过程", "after": "Mistral 把 Large 4 的“菜谱”也公开了"},
    {"before": "关 AI，就得连搜索一起关（卡片）", "after": "只想关 AI？没这个选项"},
]
# Punchy but false - never write these (they change the fact, not just the tone).
TITLE_TONE_DONTS = [
    {"text": "每 5 次搜索，ChatGPT 就分走 1 次", "why": "素材说的是“相当于 Google 的 20%”，不是抢走份额"},
    {"text": "你花钱买的 AI，可能被偷偷换成便宜货", "why": "素材只说能防止偷换，没说正在发生"},
    {"text": "欧洲最强开源模型，连研发秘方都公开了", "why": "“欧洲最强”素材里没有"},
]
# Column-style headings read as machine output; a card title must say something about this story.
GENERIC_HEADINGS = CARD_ANGLES + ("核心信息", "要点", "详情", "总结", "概述", "简介", "最新进展", "关键细节", "补充说明", "后续进展",
                                  "背景介绍", "事件回顾", "影响分析", "专家观点", "各方观点", "数据一览")
MAX_QUESTIONS_PER_STORY = 2
CARDS_PER_PAGE = 3

# Video
V6_WIDTH = 1080
V6_HEIGHT = 1920
V6_FPS = 30
V6_MIN_DURATION_SECONDS = 15.0
V6_HOOK_MAX_SECONDS = 4.0
PAUSE_AFTER_HOOK = 0.30
PAUSE_BETWEEN_SENTENCES = 0.14
PAUSE_BETWEEN_STORIES = 0.40
OUTRO_TEXT = "关注我，每天看懂 AI 大事。"
OUTRO_MIN_SECONDS = 5.5          # read the recap; the tail-aligned ending track resolves on the last frame

# Voice
TTS_PROVIDERS = ("doubao", "edge")
DEFAULT_TTS_PROVIDER = os.environ.get("AI_DAILY_TTS_PROVIDER", "doubao")
EDGE_TTS_VOICE = os.environ.get("EDGE_TTS_VOICE", "zh-CN-YunxiNeural")
EDGE_TTS_RATE = os.environ.get("EDGE_TTS_RATE", "+10%")
# Doubao: ``text_to_audio_plus`` with the fixed voice profile below, at its natural speed (no tempo change
# anywhere). Doubao reads English and Arabic numerals well, so it gets the raw sentence (display text),
# never the Azure-style normalisation ("G P T", "五千五百二十亿").
DOUBAO_TTS_TOOL = "text_to_audio_plus"
DOUBAO_TTS_VOICE_PROFILE = ("清脆元气的少女音，音调偏高，声音明亮活泼、带着少年感，语气俏皮可爱、尾音微微上扬，"
                            "语速明快、节奏轻快跳跃，像动漫角色配音一样元气满满，播报新闻时英文单词用自然的英文发音读出，"
                            "纯人声无背景音乐")
DOUBAO_TTS_REQUEST = '"{voice}"，内容如下："{spoken_text}"'
# Long takes: one request reads up to ~120 s, and one long take keeps timbre and energy steadier than
# stitched short ones. The edition is split only between stories (hook joins the first headline, the
# outro joins the last story) into as few takes as possible, balanced in length: the number of takes is
# ceil(estimate / TARGET), and every take's estimate x SAFETY must stay under MAX. The program cuts each
# take back into sentences at its pauses; a take that cannot be cut (or came back truncated) is asked
# again in two halves (between stories where possible), down to single sentences.
DOUBAO_TAKE_TARGET_SECONDS = float(os.environ.get("AI_DAILY_DOUBAO_TAKE_SECONDS", "100"))
DOUBAO_TAKE_MAX_SECONDS = 120.0
DOUBAO_TAKE_SAFETY = 1.15
DOUBAO_STORY_BREAK = "\n"            # between two stories inside one take: invites a longer pause
DOUBAO_RATE_LIMIT_WAIT_SECONDS = 10
# Speaking rate in sentence-weight units per second (a Han character = 1, see voice.sentence_weight),
# natural pauses inside a take included. Measured on every finished take and kept in
# outputs/_state/doubao_speech_rate.json (median of the last DOUBAO_RATE_HISTORY takes); this default is
# used until then. Two real Doubao takes (2026-11-04) measured 4.46 and 6.22; the default sits near the
# slow one so a first-run estimate never runs a take past 120 s.
DOUBAO_SPEECH_RATE_DEFAULT = 4.5
DOUBAO_RATE_HISTORY = 10
DOUBAO_RATE_BOUNDS = (3.0, 9.0)       # a measurement outside this range is ignored
# Every Doubao answer starts with a fixed spoken tag "内容由豆包AI生成": silence (0-0.33 s), the tag
# (0.33-2.25 s), a pause (colleague: >= 0.18 s), then the text. Rule measured on real Doubao audio (colleague,
# 2026-11-04): 10 ms frames, RMS >= 300 (16-bit) is voice; find the first voiced frame, then the first run
# of silence >= MIN_GAP after it; the text starts where that run ends. Safety check on top: that pause must
# start inside WINDOW, otherwise nothing is cut and selfcheck warns. Two real takes: the tag is one fixed
# recording (identical in both), its longest inner pause is 0.08 s and the pause after it 0.20 / 0.27 s, so
# MIN_GAP is 0.12 s (the measured 0.18 s left only 0.02 s of margin).
DOUBAO_WATERMARK_RMS_THRESHOLD = 300
DOUBAO_WATERMARK_WINDOW = (1.6, 3.0)  # seconds from the start of the answer where the tag may end
DOUBAO_WATERMARK_MIN_GAP = 0.12
DOUBAO_WATERMARK_SECONDS = 2.7        # tag + pauses, used in the take-length estimate
# Optional hard stop: an edition with fewer stories than this is not produced (0 = off, CLI --min-stories).
MIN_STORIES = int(os.environ.get("AI_DAILY_MIN_STORIES", "0"))

# Mix: music sits well under the voice and ducks further while speaking.
MIX_MUSIC_UNDER_VOICE_DB = 14.0   # bed level; ducked DUCK_EXTRA_DB (4 dB) more while speaking -> ~18 dB under the voice
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
