---
name: ai-daily-news-studio
description: 每日自动生成 AI每日早报 v6 竖屏短视频（1080×1920，60–90 秒，TikTok/小红书），含重磅选题、开头钩子、逐句配音、字幕、3:4/9:16 封面和两平台发布文案。触发词：AI 每日早报、AI早报、生成早报、每日新闻视频、今天的早报、补跑某天的早报。单入口编排、断点续跑、每一步都有自动兜底。
metadata:
  author: ZhongBintao
  version: "6.0.0"
---

# AI每日早报 v6 · 云电脑自动运行

用户只需说「生成今天的 AI 每日早报」。下面的步骤全部照做即可，**不要询问用户确认**。

## 第 0 步：准备环境（每次都做）

```bash
PROJECT_DIR="/home/user/.super_doubao/super-doubao-runtime/workspace/ai-daily-news-studio-codex-plugin"
if [ ! -d "$PROJECT_DIR/.git" ]; then git clone https://github.com/ZhongBintao/Doubao-AI-Daily-News.git "$PROJECT_DIR"; fi
cd "$PROJECT_DIR"
git pull --ff-only || echo "拉取失败，继续用本地版本"
bash scripts/bootstrap_env.sh || exit 1
source .local/env.sh
PY=OpenMontage/.venv/bin/python
```

看到 `BOOTSTRAP_OK` 再继续。

## 第 1 步：运行编排器

```bash
$PY -m ai_morning_brief.daily --date $(TZ=Asia/Shanghai date +%Y-%m-%d)
```

- 退出码 **0**：完成，跳到第 4 步。
- 退出码 **3**：需要你做一件事（AGENT HANDOFF），按屏幕提示做第 2 或第 3 步，然后**重新运行同一条命令**。
- 退出码 **1**：失败，运行 `$PY -m ai_morning_brief.daily --date 日期 --status` 查看原因（见文末“失败处理”）。

## 第 2 步（交接：director 导演稿）

1. 打开 `outputs/日期/artifacts/director_task.json`，按 `steps` 一步步做。
2. 详细写法和示范：`skills/ai-brief-editorial-writer/SKILL.md`（4 种开头写法，照着改即可）。
3. 写出 `outputs/日期/artifacts/director_plan.json`。
4. 校验：`$PY -m ai_morning_brief.daily --date 日期 --check-plan`，按报错改，最多 2 次。
5. 重新运行第 1 步的命令。校验仍失败也重新运行——程序会自动改用兜底稿。

## 第 3 步（交接：voice 逐句配音）

1. 打开 `outputs/日期/artifacts/tts_manifest.json`。
2. 对 `units` 里每一条：用 `audio_to_audio_plus`，`@音频1` = `example-audio.mp3`，prompt 用 `doubao.prompt_template`（把 `{spoken_text}` 换成该条文字），保存到该条 `output_path`。**每条一句，不要合并。**
3. 全部保存后重新运行第 1 步的命令。
4. 语音工具不可用：改用 `$PY -m ai_morning_brief.daily --date 日期 --tts edge` 运行，程序自动配音。

## 第 4 步：完成通知

```
✅ AI每日早报 YYYY-MM-DD 已生成
📹 视频：outputs/日期/release-kit/publish-package/ai-daily-news-日期.mp4（1080×1920，XX 秒）
🖼️ 封面：covers/3x4.png（小红书）、covers/9x16.png（TikTok）
📝 文案：publish-copy.md（TikTok 标题+正文+话题；小红书标题+正文）
🔎 自检：outputs/日期/artifacts/selfcheck/contact-sheet.png
⚠️ 发布前请人工过一遍视频。
```

## 可选：看图自检（不阻塞发布）

打开 `artifacts/selfcheck.json` 的 `contact_sheet` 图片，回答 `agent_checklist.questions` 的 5 个是/否问题，写入 `answers_path`。若有 no，可以只改一次 `director_plan.json` 的文字，然后运行 `$PY -m ai_morning_brief.daily --date 日期 --from director`。

## 失败处理

| 情况 | 处理 |
|---|---|
| `bootstrap` 失败 | 重新运行 `bash scripts/bootstrap_env.sh`；仍失败则报告错误并停止 |
| `fetch` 失败 | AIHOT 暂时不可用：等 5 分钟重跑一次；仍失败报告“AIHOT 暂时不可用” |
| 没有新资讯 | 程序自动生成“今日无新增重磅”短片，不补旧闻 |
| `director` 写不好 | 重新运行即可，第 2 次失败自动用兜底稿；或直接加 `--no-agent` |
| `voice` 豆包不可用 | 加 `--tts edge` 重新运行 |
| `render` 失败 | 运行 `--from render` 重试一次；仍失败报告 `run_state.json` 里的 error |
| 想完全不交接 | `$PY -m ai_morning_brief.daily --date 日期 --no-agent`（兜底稿 + edge 配音，全自动） |

任何失败都不要删除 `outputs/` 里的中间文件，重跑会自动从断点继续。

## 补跑某天

`$PY -m ai_morning_brief.daily --date 2026-10-30`。历史日期没有实时热点榜，会用当天的精选和日报选题。已成功的日期需要重做时加 `--force`。

## 说明

- 选题会自动参考 AIHOT 日报；日报每天北京时间 08:00 发布，08:00 前运行时只用精选和实时热点。
- 本 skill 只生成私有成品，**绝不自动上传或发布**。
