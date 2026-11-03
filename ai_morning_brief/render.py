"""v6 render: build the HyperFrames workspace, pre-layout it in Chrome, render MP4."""
from __future__ import annotations

import glob
import html
import json
import os
import re
import shutil
import subprocess
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from . import config as C
from .brands import trimmed_logo

TEMPLATE = Path(__file__).resolve().parent / "template" / "v6" / "index.html"
HYPERFRAMES_PACKAGE = os.environ.get("HYPERFRAMES_PACKAGE", "hyperframes@0.9.8")
ICONS = {
    "spark": '<svg viewBox="0 0 48 48"><path d="M24 4l3.6 13.2L41 21l-13.4 3.8L24 38l-3.6-13.2L7 21l13.4-3.8L24 4Z"/></svg>',
    "bars": '<svg viewBox="0 0 48 48"><path d="M9 39V25h7v14H9Zm12 0V12h7v27h-7Zm12 0V6h7v33h-7Z"/></svg>',
    "chart": '<svg viewBox="0 0 48 48"><rect x="7" y="7" width="34" height="34" rx="3"/><path d="M14 33v-8M23 33V15M32 33v-13"/></svg>',
    "clock": '<svg viewBox="0 0 48 48"><circle cx="24" cy="25" r="14"/><path d="M24 17v9l6 4M18 7h12"/></svg>',
    "code": '<svg viewBox="0 0 48 48"><path d="m19 14-10 11 10 11M29 14l10 11-10 11"/></svg>',
    "alert": '<svg viewBox="0 0 48 48"><path d="M24 6 44 40H4L24 6Z"/><path d="M24 19v10M24 34v1"/></svg>',
}


HL_OPEN, HL_CLOSE = '<span class="hl">', "</span>"


class RenderError(RuntimeError):
    pass


def icon_for(text: str, index: int) -> str:
    if re.search(r"待验证|尚无|风险|窃取|攻击|不受影响|抗议|起诉|下调|削减|亏损", text):
        return "alert"
    if re.search(r"\d|%|价格|额度|成绩|营收|参数", text):
        return "bars"
    if re.search(r"研究|论文|方法|做法|原理", text):
        return "chart"
    if re.search(r"月|日|时间|生效|起|计划", text):
        return "clock"
    return ("spark", "chart", "code")[index % 3]


def _marked(text: Any, open_tag: str, close_tag: str) -> str:
    escaped = html.escape(str(text or ""))
    return re.sub(r"【(.+?)】", lambda m: f"{open_tag}{m.group(1)}{close_tag}", escaped).replace("【", "").replace("】", "")


def _subject_chip(scene: Mapping[str, Any], logos: Mapping[str, str]) -> str:
    logo = logos.get(scene["scene_id"])
    if logo:
        return f'<span class="subject"><img src="{html.escape(logo)}" alt="{html.escape(str(scene.get("subject") or ""))}"></span>'
    return f'<span class="subject">{html.escape(str(scene.get("subject") or ""))}</span>'


def _header(scene: Mapping[str, Any], run_date: date) -> str:
    progress = scene.get("progress") or None
    right = ""
    if progress:
        segs = "".join(f'<i class="{"on" if k < progress["index"] else ""}"></i>' for k in range(progress["total"]))
        right = f'<div class="progress">{html.escape(progress["role"])} {progress["index"]}/{progress["total"]}<span class="segs">{segs}</span></div>'
    return (f'<div class="header"><div class="wordmark"><span class="rule"></span>{html.escape(C.DEFAULT_SHOW_NAME)}'
            f'<span class="date">{run_date.month:02d}.{run_date.day:02d}</span></div>{right}</div>')


def scene_markup(scene: Mapping[str, Any], run_date: date, logos: Mapping[str, str]) -> str:
    kind = scene["kind"]
    cat = re.sub(r"[^a-z\-]", "", str(scene.get("category") or "other")) or "other"
    head = _header(scene, run_date)
    sid = html.escape(scene["scene_id"])
    if kind == "hook":
        return (f'<section class="scene kind-hook cat-{cat}" id="{sid}"><div class="orbit"></div>{head}<div class="body">'
                f'<div class="kicker-row"><span class="kicker">今日头条</span>{_subject_chip(scene, logos)}</div>'
                f'<h1 class="hook-title fit" data-max="118" data-min="76" data-lines="4">{_marked(scene.get("hook"), "<em>", "</em>")}</h1>'
                f'<div class="hook-rule"></div><div class="keyfact"><div class="label">发生了什么</div><p>{html.escape(str(scene.get("keyfact") or ""))}</p></div>'
                f'</div></section>')
    if kind == "story":
        cards = []
        for i, card in enumerate(scene.get("cards") or []):
            text = f'{card.get("label", "")}{card.get("headline", "")}{card.get("body", "")}'
            cards.append(f'<article class="card" data-card="{i + 1}"><div class="icon">{ICONS[icon_for(text, i)]}</div><div class="copy">'
                         f'<div class="label">{html.escape(str(card.get("label") or ""))}</div><h3>{html.escape(str(card.get("headline") or ""))}</h3>'
                         f'<p>{_marked(card.get("body"), HL_OPEN, HL_CLOSE)}</p></div></article>')
        return (f'<section class="scene kind-story cat-{cat}" id="{sid}">{head}<div class="body">'
                f'<div class="kicker-row"><span class="section">{html.escape(str(scene.get("section") or ""))}</span></div>'
                f'<h2 class="story-title fit" data-max="68" data-min="50" data-lines="3">{html.escape(str(scene.get("title") or ""))}</h2>'
                f'<div class="story-meta">{_subject_chip(scene, logos)}</div>'
                f'<div class="cards"><div class="cards-inner">{"".join(cards)}</div></div></div></section>')
    if kind == "flash":
        return (f'<section class="scene kind-flash cat-{cat}" id="{sid}">{head}<div class="body">'
                f'<div class="kicker-row"><span class="kicker teal">快讯</span><span class="section">{html.escape(str(scene.get("section") or ""))}</span></div>'
                f'<h2 class="story-title fit" data-max="76" data-min="54" data-lines="3" style="margin-top:34px">{html.escape(str(scene.get("title") or ""))}</h2>'
                f'<div class="story-meta">{_subject_chip(scene, logos)}</div>'
                f'<div class="flash-card"><div class="label">要点</div><p>{html.escape(str(scene.get("point") or ""))}</p></div></div></section>')
    return (f'<section class="scene kind-outro cat-ai-models" id="{sid}"><div class="orbit"></div>{head}<div class="body">'
            f'<div class="outro-mark">{html.escape(C.DEFAULT_SHOW_NAME)}</div><div class="outro-rule"></div>'
            f'<div class="outro-text">{html.escape(str(scene.get("text") or C.OUTRO_TEXT))}</div>'
            f'<div class="outro-sub">资讯来源：AIHOT · 明天见</div></div></section>')


def ensure_font(destination_dir: Path) -> Path | None:
    """Copy the cached Noto Sans SC VF font into the workspace, downloading once if needed."""

    cached = C.FONT_CACHE_DIR / C.FONT_FILENAME
    if not cached.is_file() or cached.stat().st_size < 1_000_000:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(C.FONT_URL, timeout=180) as response:
                cached.write_bytes(response.read())
        except Exception:
            return None
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / C.FONT_FILENAME
    if not target.is_file() or target.stat().st_size != cached.stat().st_size:
        shutil.copy2(cached, target)
    return target


def find_chrome() -> str | None:
    for name in ("CHROME_PATH", "CHROME_BIN", "PUPPETEER_EXECUTABLE_PATH"):
        if os.environ.get(name) and Path(os.environ[name]).is_file():
            return os.environ[name]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for pattern in ("/opt/pw-browsers/chromium*/chrome-linux*/chrome", str(Path.home() / ".cache/ms-playwright/chromium*/chrome-linux*/chrome"),
                    str(Path.home() / ".cache/hyperframes/**/chrome"),
                    str(Path.home() / ".cache/hyperframes/**/chrome-headless-shell"), str(Path.home() / ".cache/puppeteer/**/chrome-headless-shell"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        matches = sorted(m for m in glob.glob(pattern, recursive=True) if os.path.isfile(m) and os.access(m, os.X_OK))
        if matches:
            return matches[-1]
    try:  # HyperFrames manages its own Chrome download
        result = subprocess.run(["npx", "--yes", HYPERFRAMES_PACKAGE, "browser", "path"], capture_output=True, text=True, timeout=180, check=False)
        candidate = (result.stdout or "").strip().splitlines()[-1] if result.stdout.strip() else ""
        if candidate and os.path.isfile(candidate):
            return candidate
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    return None


def materialize(run_dir: Path, final_plan: Mapping[str, Any], timeline: Mapping[str, Any], run_date: date) -> Path:
    workspace = run_dir / "hyperframes"
    if workspace.exists():
        shutil.rmtree(workspace)
    (workspace / "assets" / "audio").mkdir(parents=True)
    shutil.copy2(run_dir / "assets" / "audio" / "final-mix.wav", workspace / "assets" / "audio" / "final-mix.wav")
    font = ensure_font(workspace / "assets" / "fonts")
    logos: dict[str, str] = {}
    for scene in final_plan.get("scenes") or []:
        source = scene.get("logo")
        if source and Path(source).is_file():
            target = workspace / "assets" / "logos" / Path(source).name
            if not target.exists():
                trimmed_logo(Path(source), target)
            logos[scene["scene_id"]] = f"assets/logos/{target.name}"
    timings = {s["scene_id"]: s for s in timeline.get("scenes") or []}
    body = [scene_markup(scene, run_date, logos) for scene in final_plan.get("scenes") or [] if scene["scene_id"] in timings]
    captions = "".join(
        f'<div class="cap" data-start="{cue["start"]:.3f}" data-end="{cue["end"]:.3f}"><span>{html.escape(cue["text"])}</span></div>'
        for cue in timeline.get("captions") or [])
    body.append(f'<div class="captions">{captions}</div>')
    data = {"total_duration": timeline["total_duration"], "scenes": [timings[s["scene_id"]] | {"kind": s["kind"]} for s in final_plan["scenes"] if s["scene_id"] in timings],
            "card_events": timeline.get("card_events") or {}}
    page = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__SHOW_NAME__", C.DEFAULT_SHOW_NAME).replace("__DATE__", run_date.isoformat())
            .replace("__TOTAL__", f'{timeline["total_duration"]:.3f}').replace("__BODY__", "\n".join(body))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    (workspace / "index.html").write_text(page, encoding="utf-8")
    (workspace / "hyperframes.json").write_text(json.dumps({"project": f"ai-daily-news-{run_date.isoformat()}", "width": C.V6_WIDTH, "height": C.V6_HEIGHT,
                                                            "fps": C.V6_FPS, "template_id": "ai-daily-news-v6", "display_name": C.DEFAULT_SHOW_NAME}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "artifacts" / "render_inputs.json").write_text(json.dumps({"font_bundled": bool(font), "logos": logos}, ensure_ascii=False, indent=2), encoding="utf-8")
    return workspace


def layout_prepass(workspace: Path) -> dict[str, Any]:
    """Run the template's layout script in headless Chrome and freeze the result."""

    chrome = find_chrome()
    if not chrome:
        return {"status": "skipped", "reason": "no Chrome/Chromium found; default font sizes are used"}
    index = workspace / "index.html"
    cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
           "--window-size=1080,1920", "--virtual-time-budget=20000", "--run-all-compositor-stages-before-draw", "--dump-dom",
           index.resolve().as_uri() + "#layout"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    dom = result.stdout
    match = re.search(r'<html[^>]*data-layout="([^"]*)"', dom)
    if result.returncode != 0 or not match or 'id="layout-script"' in dom:
        return {"status": "skipped", "reason": f"layout prepass did not finish (exit {result.returncode})", "stderr": result.stderr[-500:]}
    report = json.loads(html.unescape(match.group(1)))
    dom = re.sub(r'\sdata-layout="[^"]*"', "", dom, count=1)
    index.write_text("<!doctype html>\n" + dom, encoding="utf-8")
    report["status"] = "done"
    return report


def render(workspace: Path, output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["npx", "--yes", HYPERFRAMES_PACKAGE, "render", str(workspace), "--output", str(output), "--fps", str(C.V6_FPS), "--quality", "standard", "--quiet"]
    result = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True, timeout=3600, check=False)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise RenderError(f"HyperFrames render failed (exit {result.returncode}): {(result.stderr or result.stdout)[-1200:]}")
    return {"command": " ".join(cmd[:4]), "stdout_tail": result.stdout[-800:]}


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-show_entries",
                             "stream=codec_type,codec_name,width,height,sample_rate,channels", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RenderError("ffprobe failed on the rendered video")
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise RenderError("rendered MP4 must contain video and audio")
    if (video.get("width"), video.get("height")) != (C.V6_WIDTH, C.V6_HEIGHT):
        raise RenderError(f"rendered MP4 must be {C.V6_WIDTH}x{C.V6_HEIGHT}, got {video.get('width')}x{video.get('height')}")
    return {"duration": float(data["format"]["duration"]), "size": int(data["format"]["size"]), "width": video["width"], "height": video["height"],
            "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name")}
