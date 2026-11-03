"""v6 publish kit: hook-style titles, hashtags and an atomic package.

Nothing is uploaded. The package holds the MP4, covers, platform copy and a
manifest with SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from . import config as C

BASE_TAGS = ["#AI", "#人工智能", "#AI资讯", "#科技新闻"]
CATEGORY_TAGS = {"ai-models": "#大模型", "ai-products": "#AI工具", "industry": "#科技行业", "paper": "#AI研究"}
XHS_TITLE_MAX = 20


def _plain(text: Any) -> str:
    return re.sub(r"[【】]", "", str(text or "")).strip()


def _short(text: str, maximum: int) -> str:
    """Shorten at punctuation; never leave an unclosed bracket or a cut word."""

    if len(text) <= maximum:
        return text
    out = ""
    for piece in re.split(r"(?<=[，,：:；;、）)])|(?=[（(])", text):
        if len(out + piece) > maximum:
            break
        out += piece
    out = out.rstrip("，,：:；;、 ")
    if out.count("（") > out.count("）"):
        out = out[: out.rfind("（")].rstrip()
    if len(out) >= maximum // 2:
        return out
    cut = ""
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9.+#/\-]*|\s+|.", text):
        if len(cut + match.group(0)) > maximum:
            break
        cut += match.group(0)
    return cut.rstrip()


def build_copy(run_date: date, final_plan: Mapping[str, Any], duration: float) -> dict[str, Any]:
    scenes = final_plan.get("scenes") or []
    hook = next((s for s in scenes if s["kind"] == "hook"), {})
    stories = [s for s in scenes if s["kind"] in {"story", "flash"}]
    hook_text = _plain(hook.get("hook"))
    tags: list[str] = []
    for story in stories:
        subject = re.sub(r"\s+", "", str(story.get("subject") or ""))
        if subject and f"#{subject}" not in tags and len(subject) <= 16:
            tags.append(f"#{subject}")
        tag = CATEGORY_TAGS.get(str(story.get("category") or ""))
        if tag and tag not in tags:
            tags.append(tag)
    hashtags = BASE_TAGS + [t for t in tags if t not in BASE_TAGS][:6]
    lines = [f"{i + 1}. {_plain(s.get('title'))}" for i, s in enumerate(stories)]
    date_label = f"{run_date.month}月{run_date.day}日"
    tiktok_title = f"{hook_text}｜AI每日早报 {run_date.month:02d}.{run_date.day:02d}"
    body = "\n".join([f"{date_label} AI 要闻，{max(1, round(duration))} 秒看完：", *lines, "", _plain(hook.get("keyfact")), "", "关注我，每天一分钟看懂 AI。"])
    return {
        "date": run_date.isoformat(),
        "tiktok": {"caption": f"{tiktok_title}\n\n{body}\n\n{' '.join(hashtags)}", "title": tiktok_title},
        "xiaohongshu": {"title": _short(hook_text, XHS_TITLE_MAX), "body": body + "\n\n" + " ".join(hashtags), "cover": "covers/3x4.png"},
        "hashtags": hashtags,
        "source_note": "资讯来源：AIHOT（aihot.news）",
    }


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def assemble(run_dir: Path, run_date: date, final_plan: Mapping[str, Any], video: Path, duration: float) -> dict[str, Any]:
    kit = run_dir / "release-kit"
    target = kit / "publish-package"
    copy = build_copy(run_date, final_plan, duration)
    staging = Path(tempfile.mkdtemp(prefix=".package-", dir=kit))
    try:
        (staging / "covers").mkdir()
        shutil.copy2(video, staging / video.name)
        for ratio in ("3x4", "9x16"):
            cover = kit / "covers" / f"{ratio}.png"
            if cover.is_file():
                shutil.copy2(cover, staging / "covers" / cover.name)
        subtitles = run_dir / "assets" / "subtitles" / "subtitles.srt"
        if subtitles.is_file():
            shutil.copy2(subtitles, staging / "subtitles.srt")
        md = [f"# AI每日早报 {run_date.isoformat()} 发布文案", "", "## TikTok", "", "```", copy["tiktok"]["caption"], "```", "",
              "## 小红书", "", f"**标题**：{copy['xiaohongshu']['title']}", "", "```", copy["xiaohongshu"]["body"], "```", "",
              f"封面：TikTok 用 `covers/9x16.png`，小红书用 `covers/3x4.png`。", "", copy["source_note"], ""]
        (staging / "publish-copy.md").write_text("\n".join(md), encoding="utf-8")
        (staging / "publish-copy.json").write_text(json.dumps(copy, ensure_ascii=False, indent=2), encoding="utf-8")
        files = {p.relative_to(staging).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size} for p in sorted(staging.rglob("*")) if p.is_file()}
        manifest = {"version": C.V6_VERSION, "date": run_date.isoformat(), "files": files, "uploaded": False}
        (staging / "package.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {"path": str(target.relative_to(run_dir)), "copy": copy}
