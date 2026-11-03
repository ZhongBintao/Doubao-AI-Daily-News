"""v6 covers: deterministic HTML covers in the video's visual language.

3:4 (1080x1440) for 小红书 and 9:16 (1080x1920) for TikTok are rendered with
headless Chrome. No image model is required, so covers never block an edition.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from . import config as C
from .render import find_chrome

RATIOS = {"3x4": (1080, 1440), "9x16": (1080, 1920)}

COVER_CSS = """
@font-face { font-family: "Noto Sans SC"; src: url("__FONT__") format("truetype"); font-weight: 100 900; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: __W__px; height: __H__px; overflow: hidden; }
body { font-family: "Noto Sans SC", "WenQuanYi Zen Hei", sans-serif; background: #f7f4ee; color: #20282b; position: relative; }
body::before { content: ""; position: absolute; inset: 0; opacity: .55; background-image: radial-gradient(circle at 86% 10%, rgba(207,112,74,.22), rgba(207,112,74,0) 36%),
  linear-gradient(rgba(32,40,43,.045) 1px, transparent 1px), linear-gradient(90deg, rgba(32,40,43,.045) 1px, transparent 1px); background-size: auto, 72px 72px, 72px 72px; }
.orbit { position: absolute; width: 820px; height: 820px; right: -300px; top: 60px; border: 2px solid rgba(63,115,118,.18); border-radius: 50%; }
.orbit::before { content: ""; position: absolute; inset: 90px; border: 2px dashed rgba(207,112,74,.26); border-radius: 50%; }
.wrap { position: absolute; left: 72px; right: 72px; top: __TOP__px; bottom: __BOTTOM__px; display: flex; flex-direction: column; }
.mast { display: flex; align-items: center; justify-content: space-between; }
.mark { display: flex; align-items: center; gap: 16px; color: #cf704a; font: 900 50px/1 "Noto Sans SC"; }
.mark i { width: 14px; height: 50px; border-radius: 99px; background: #cf704a; }
.date { font: 800 40px/1 "Noto Sans SC"; color: #3f7376; letter-spacing: .04em; }
.main { flex: 1; display: flex; flex-direction: column; justify-content: center; }
.row { display: flex; align-items: center; gap: 18px; margin-bottom: 40px; }
.kicker { padding: 14px 26px; border-radius: 16px; background: #cf704a; color: #fff; font: 900 40px/1 "Noto Sans SC"; letter-spacing: .08em; }
.subject { display: inline-flex; align-items: center; height: 92px; padding: 0 28px; border-radius: 22px; background: #fffefa; border: 1px solid #dedbd2; font: 900 44px/1 "Noto Sans SC"; box-shadow: 0 12px 28px rgba(32,40,43,.08); }
.subject img { height: 52px; }
h1 { font-weight: 900; font-size: 132px; line-height: 1.1; letter-spacing: -.02em; text-wrap: balance; }
h1 em { font-style: normal; color: #cf704a; }
.rule { width: 120px; height: 12px; border-radius: 99px; background: #cf704a; margin: 48px 0 40px; }
.list { background: #fffefa; border: 1px solid #dedbd2; border-radius: 30px; padding: 32px 36px; box-shadow: 0 18px 36px rgba(32,40,43,.09); }
.list .label { color: #3f7376; font: 800 32px/1 "Noto Sans SC"; letter-spacing: .06em; margin-bottom: 18px; }
.list li { list-style: none; display: flex; gap: 16px; font: 700 38px/1.35 "Noto Sans SC"; margin-top: 12px; }
.list li b { color: #cf704a; }
.foot { font: 800 34px/1.3 "Noto Sans SC"; color: #6d7371; letter-spacing: .04em; }
.w { white-space: nowrap; }
"""

FIT_JS = """
(function(){
  const seg = Intl.Segmenter ? new Intl.Segmenter("zh", {granularity: "word"}) : null;
  const ASCII = /^[A-Za-z0-9.\\-+%#_/]+$/, PUNCT = /^[，。！？、：；,.!?:;”’）)》%％]+$/, HAN1 = /^[\\u4e00-\\u9fff]$/;
  const wrap = (el) => { if (!seg) return; [...el.childNodes].forEach((c) => { if (c.nodeType === 3) { const f = document.createDocumentFragment(); let prev = null, pair = false;
    for (const {segment} of seg.segment(c.textContent)) { if (/^\\s+$/.test(segment)) { f.appendChild(document.createTextNode(segment)); prev = null; pair = false; continue; }
      if (prev && ((ASCII.test(prev.textContent) && ASCII.test(segment)) || PUNCT.test(segment) || (pair && HAN1.test(segment)))) { prev.textContent += segment; pair = false; continue; }
      const s = document.createElement("span"); s.className = "w"; s.textContent = segment; f.appendChild(s); prev = s; pair = HAN1.test(segment); } c.replaceWith(f); } else if (c.nodeType === 1) wrap(c); }); };
  document.fonts.ready.then(() => { const h = document.querySelector("h1"); wrap(h);
    for (let size = __MAX__; size >= 76; size -= 4) { h.style.fontSize = size + "px"; const lh = parseFloat(getComputedStyle(h).lineHeight); if (h.scrollHeight <= lh * __LINES__ + 2) break; }
    document.body.dataset.ready = "1"; });
})();
"""


def _marked(text: str) -> str:
    escaped = html.escape(text)
    return re.sub(r"【(.+?)】", r"<em>\1</em>", escaped).replace("【", "").replace("】", "")


def cover_html(ratio: str, *, run_date: date, final_plan: Mapping[str, Any], font_uri: str, logo_uri: str, duration: float) -> str:
    width, height = RATIOS[ratio]
    scenes = final_plan.get("scenes") or []
    hook = next((s for s in scenes if s["kind"] == "hook"), {})
    others = [s for s in scenes if s["kind"] in {"story", "flash"}][1:4]
    subject = f'<img src="{html.escape(logo_uri)}">' if logo_uri else html.escape(str(hook.get("subject") or ""))
    items = "".join(f"<li><b>{i + 2:02d}</b><span>{html.escape(str(s.get('title') or ''))}</span></li>" for i, s in enumerate(others))
    tall = ratio == "9x16"
    css = (COVER_CSS.replace("__FONT__", font_uri).replace("__W__", str(width)).replace("__H__", str(height))
           .replace("__TOP__", "230" if tall else "90").replace("__BOTTOM__", "520" if tall else "90"))
    js = FIT_JS.replace("__MAX__", "132").replace("__LINES__", "4" if tall else "3")
    count = 1 + len([s for s in scenes if s["kind"] in {"story", "flash"}][1:])
    foot = f"今日 {count} 条 AI 要闻 · {max(1, round(duration))} 秒看完"
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body><div class="orbit"></div><div class="wrap">'
            f'<div class="mast"><div class="mark"><i></i>{html.escape(C.DEFAULT_SHOW_NAME)}</div><div class="date">{run_date.year}.{run_date.month:02d}.{run_date.day:02d}</div></div>'
            f'<div class="main"><div class="row"><span class="kicker">今日头条</span><span class="subject">{subject}</span></div>'
            f'<h1>{_marked(str(hook.get("hook") or ""))}</h1><div class="rule"></div>'
            + (f'<ul class="list"><div class="label">还有这些</div>{items}</ul>' if items else "")
            + f'</div><div class="foot">{html.escape(foot)}</div></div><script>{js}</script></body></html>')


def make_covers(run_dir: Path, run_date: date, final_plan: Mapping[str, Any], duration: float) -> dict[str, Any]:
    chrome = find_chrome()
    out_dir = run_dir / "release-kit" / "covers"
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "hyperframes"
    font = workspace / "assets" / "fonts" / C.FONT_FILENAME
    hook = next((s for s in final_plan.get("scenes") or [] if s["kind"] == "hook"), {})
    logo = ""
    logo_dir = workspace / "assets" / "logos"
    if hook.get("logo") and (logo_dir / Path(hook["logo"]).name).is_file():
        logo = (logo_dir / Path(hook["logo"]).name).resolve().as_uri()
    results: dict[str, Any] = {}
    for ratio, (width, height) in RATIOS.items():
        page = out_dir / f"cover-{ratio}.html"
        page.write_text(cover_html(ratio, run_date=run_date, final_plan=final_plan, font_uri=font.resolve().as_uri() if font.is_file() else "",
                                   logo_uri=logo, duration=duration), encoding="utf-8")
        png = out_dir / f"{ratio}.png"
        if not chrome:
            results[ratio] = {"status": "skipped", "reason": "no Chrome/Chromium"}
            continue
        cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
               f"--window-size={width},{height}", "--virtual-time-budget=8000", f"--screenshot={png}", page.resolve().as_uri()]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        results[ratio] = {"status": "done" if png.is_file() and png.stat().st_size > 0 else "failed", "path": str(png.relative_to(run_dir)),
                          "size": [width, height], **({} if result.returncode == 0 else {"stderr": result.stderr[-300:]})}
    manifest = {"version": C.V6_VERSION, "engine": "html-chrome", "covers": results}
    (out_dir / "cover_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
