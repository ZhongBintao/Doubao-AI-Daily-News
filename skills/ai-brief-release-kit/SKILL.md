---
name: ai-brief-release-kit
description: AI每日早报 v6 publish kit — hook-style TikTok / 小红书 titles, hashtags and an atomic package (MP4, covers, copy, subtitles, hashes). Generated automatically by the publish stage; never uploads.
metadata:
  author: local-project
  version: "6.0.0"
---

# AI每日早报 v6 · 发布文案与整合包

编排器 `publish` 阶段自动完成，无需手动步骤；本 skill 说明产出与规则。

## 产出：`outputs/YYYY-MM-DD/release-kit/publish-package/`

- `ai-daily-news-YYYY-MM-DD.mp4`（1080×1920 成片）
- `covers/3x4.png`（小红书）、`covers/9x16.png`（TikTok）
- `publish-copy.md`（可直接复制的两平台文案）与 `publish-copy.json`
- `subtitles.srt`
- `package.json`（每个文件的 SHA-256 与大小，`uploaded: false`）

## 文案规则

- **TikTok 标题**：`<钩子>｜AI每日早报 MM.DD`；正文为“M月D日 AI 要闻，X 秒看完”+ 编号新闻标题 + 头条事实 + 关注引导 + 话题标签。
- **小红书标题**：钩子去掉【】后 ≤20 字，在标点处截断，不截断英文词，不留半个括号。
- **话题标签**：`#AI #人工智能 #AI资讯 #科技新闻` + 各新闻主体（如 `#OpenAI`）+ 类别标签（如 `#大模型`），最多 10 个。
- 所有内容只来自导演稿与 AIHOT；末尾注明“资讯来源：AIHOT（aihot.news）”。

## 规则

- 整合包使用临时目录组装后原子替换，可重复执行：`python -m ai_morning_brief.daily --date YYYY-MM-DD --from publish`。
- 发布阶段同时把本期新闻写入 `outputs/_state/aired_events.json`，供后续去重。
- **绝不自动上传或发布到任何平台。**
