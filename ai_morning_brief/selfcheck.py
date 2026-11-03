"""v6 self-check: objective gates plus a yes/no checklist for the agent.

Objective checks never call a model. The checklist (5 yes/no questions on a
contact sheet) is optional and non-blocking: an agent may answer it and, at
most once, edit text in director_plan.json and re-run from the director stage.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from . import config as C

QUESTIONS = [
    {"id": "q1", "question": "第 1 帧（0 秒）能否一眼看出今天的头条讲的是什么？"},
    {"id": "q2", "question": "所有画面里有没有文字被遮挡、重叠或超出画面？（没有请答 yes）"},
    {"id": "q3", "question": "卡片和字幕说的是同一件事吗？"},
    {"id": "q4", "question": "画面里的公司名、数字和新闻事实看起来正确吗？"},
    {"id": "q5", "question": "整体看起来像正式发布的资讯视频，而不是测试画面吗？"},
]


def _frame(video: Path, at: float, out: Path, width: int = 360) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-1", str(out)],
                            capture_output=True, timeout=60, check=False)
    return result.returncode == 0 and out.is_file()


def _not_blank(path: Path) -> bool:
    from PIL import Image, ImageStat

    stat = ImageStat.Stat(Image.open(path).convert("L"))
    return stat.stddev[0] > 12


def contact_sheet(frames: list[tuple[str, Path]], out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font_path = C.FONT_CACHE_DIR / C.FONT_FILENAME
    try:
        font = ImageFont.truetype(str(font_path), 20) if font_path.is_file() else ImageFont.load_default()
    except OSError:
        font = ImageFont.load_default()
    images = [(label, Image.open(p).convert("RGB")) for label, p in frames if p.is_file()]
    if not images:
        return
    w, h = images[0][1].size
    cols = min(4, len(images))
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + 12) + 12, rows * (h + 44) + 12), (40, 40, 44))
    draw = ImageDraw.Draw(sheet)
    for i, (label, image) in enumerate(images):
        x, y = 12 + (i % cols) * (w + 12), 12 + (i // cols) * (h + 44)
        sheet.paste(image, (x, y + 30))
        draw.text((x, y + 4), label, fill=(230, 230, 230), font=font)
    sheet.save(out)


def run(run_dir: Path, video: Path, probe: Mapping[str, Any], timeline: Mapping[str, Any], *, layout: Mapping[str, Any] | None = None, mix: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out_dir = run_dir / "artifacts" / "selfcheck"
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    check("resolution", (probe.get("width"), probe.get("height")) == (C.V6_WIDTH, C.V6_HEIGHT), f'{probe.get("width")}x{probe.get("height")}')
    check("audio_stream", bool(probe.get("audio_codec")), str(probe.get("audio_codec")))
    duration = float(probe.get("duration") or 0)
    check("duration", 15 <= duration <= C.V6_MAX_DURATION_SECONDS, f"{duration:.1f}s（上限 {C.V6_MAX_DURATION_SECONDS:.0f}s）")
    scenes = list(timeline.get("scenes") or [])
    hook = next((s for s in scenes if s["kind"] == "hook"), None)
    hook_len = (hook["end"] - hook["start"]) if hook else 0
    check("hook_length", bool(hook) and hook_len <= C.V6_HOOK_MAX_SECONDS + 0.5, f"开头钩子 {hook_len:.1f}s")
    # The hook is shown in large type instead of a caption, so coverage counts the captioned units only.
    speech = sum(float(u["duration"]) for u in timeline.get("units") or [] if u.get("caption", True) is not False)
    covered = sum(float(c["end"]) - float(c["start"]) for c in timeline.get("captions") or [] if c.get("onscreen", True))
    check("caption_coverage", speech > 0 and covered / speech >= 0.9, f"字幕覆盖 {covered / max(speech, 0.01):.0%} 的语音时长（钩子页用大字代替字幕）")
    if layout is not None:
        check("layout", layout.get("status") == "done" and not layout.get("overflow") and not layout.get("caption_overflow"),
              f'预排版 {layout.get("status")}，溢出 {layout.get("overflow") or []}，超过两行的字幕 {layout.get("caption_overflow") or []}')
    if mix is not None:
        check("mix", mix.get("status") == "pass", f'{(mix.get("final") or {}).get("lufs")} LUFS，说话时音乐低于人声 {mix.get("music_under_voice_while_speaking_db")} dB')
    frames: list[tuple[str, Path]] = []
    marks = [("0.0s 第一帧", 0.0), ("1.5s", 1.5), ("3.0s", 3.0)]
    for s in scenes[1:]:
        marks.append((f'{s["scene_id"]} {(s["start"] + s["end"]) / 2:.1f}s', (s["start"] + s["end"]) / 2))
    for i, (label, at) in enumerate(marks):
        path = out_dir / f"frame-{i:02d}.png"
        if _frame(video, min(at, max(0.0, duration - 0.05)), path):
            frames.append((label, path))
    if frames:
        check("first_frame_has_content", _not_blank(frames[0][1]), "第一帧不是空白画面")
        contact_sheet(frames, out_dir / "contact-sheet.png")
    status = "pass" if all(c["status"] == "pass" for c in checks) else "fail"
    report = {
        "version": C.V6_VERSION,
        "status": status,
        "checks": checks,
        "contact_sheet": str((out_dir / "contact-sheet.png").relative_to(run_dir)) if frames else None,
        "frames": [str(p.relative_to(run_dir)) for _, p in frames],
        "agent_checklist": {
            "optional": True,
            "instructions": "打开 contact_sheet 图片，逐题只回答 yes 或 no，写入 answers_path。若有 no，可以只修改 director_plan.json 里的文字一次，然后运行 rerun_command；不改也能发布。",
            "answers_path": str(out_dir / "answers.json"),
            "questions": QUESTIONS,
        },
    }
    (run_dir / "artifacts" / "selfcheck.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
