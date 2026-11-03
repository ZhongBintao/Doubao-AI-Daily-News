---
name: ai-brief-cover-generator-doubao
description: AI每日早报 v6 covers — 3:4 (小红书) and 9:16 (TikTok) covers rendered automatically from the director plan with the video's own HTML visual system. No image model or agent step is required.
metadata:
  author: local-project
  version: "3.0.0"
---

# AI每日早报 v6 · 封面

v6 封面由编排器的 `cover` 阶段**自动生成**，不需要调用 image_edit 或任何图像模型，也不需要你动手。

## 产出

- `outputs/YYYY-MM-DD/release-kit/covers/3x4.png`（1080×1440，小红书首图）
- `outputs/YYYY-MM-DD/release-kit/covers/9x16.png`（1080×1920，TikTok 封面；底部约 520 px 留给平台标题）
- `cover_manifest.json`，以及可复查的 `cover-3x4.html` / `cover-9x16.html`

## 内容（全部来自导演稿，不补写事实）

- 字标 `AI每日早报` + 日期
- “今日头条”标签 + 主体 Logo（品牌库有官方 Logo 时）或主体名
- 头条钩子大字（导演稿 `hook.text`，【】部分为强调色）
- “还有这些”：其余 1–3 条新闻标题
- 底部：“今日 N 条 AI 要闻 · X 秒看完”（N、X 取自实际成片）

## 视觉

与视频同一套 v6 设计：米白网格纸张底、陶土橙字标与强调、深青标签、白色圆角卡片、Noto Sans SC。版式规格见 `docs/design/v6-vertical-template.md`。

## 需要重做封面时

```bash
python -m ai_morning_brief.daily --date YYYY-MM-DD --from cover
```

没有 Chrome 时该阶段会标记 `skipped`，不影响视频与发布包。
