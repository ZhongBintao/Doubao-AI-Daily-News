---
name: ai-daily-news-studio
description: 每日自动生成 AI每日早报 v6 竖屏资讯视频（1080×1920，时长随内容、上限 3 分钟，TikTok/小红书），含重磅选题、抓眼球标题、逐句配音、字幕、3:4/9:16 封面和两平台发布文案。触发词：AI 每日早报、AI早报、生成早报、每日新闻视频、今天的早报、补跑某天的早报。单入口编排、断点续跑、每一步都有自动兜底。
metadata:
  author: ZhongBintao
  version: "6.2.0"
---

# AI每日早报 v6 · 云电脑自动运行

用户只需说「生成今天的 AI 每日早报」。下面的步骤全部照做即可，**不要询问用户确认**。日期一律按 Asia/Shanghai 取。

## 第 0 步：准备环境（每次都做）

```bash
PROJECT_DIR="/home/user/.super_doubao/super-doubao-runtime/workspace/ai-daily-news-studio-codex-plugin"
BRANCH="design/v6-vertical-template"
if [ ! -d "$PROJECT_DIR/.git" ]; then git clone https://github.com/ZhongBintao/Doubao-AI-Daily-News.git "$PROJECT_DIR"; fi
cd "$PROJECT_DIR"
git fetch origin && git checkout "$BRANCH" && git pull --ff-only origin "$BRANCH"
bash scripts/bootstrap_env.sh || exit 1      # 必须看到 BOOTSTRAP_OK
source .local/env.sh
PY=OpenMontage/.venv/bin/python
DAY=$(TZ=Asia/Shanghai date +%Y-%m-%d)
```

v6 不需要 faster-whisper（字幕按句对齐），不要安装。

## 第 1 步：运行编排器

```bash
$PY -m ai_morning_brief.daily --date $DAY --min-stories 3
```

- 退出码 **0**：完成，跳到第 4 步。
- 退出码 **3**：需要你做一件事（AGENT HANDOFF），按屏幕提示做第 2 或第 3 步，然后**重新运行同一条命令**。
- 退出码 **4**：今天可播的新闻不足 3 条，**按要求终止**，向用户报告原因（屏幕提示里有），不要用旧闻或虚构内容填充。
- 退出码 **1**：失败，运行 `$PY -m ai_morning_brief.daily --date $DAY --status` 查看原因（见文末“失败处理”）。

## 第 2 步（交接：director 导演稿）

1. 打开 `outputs/$DAY/artifacts/director_task.json`，按 `steps` 一步步做。
2. 写法、标题套路和语气、示范：`skills/ai-brief-editorial-writer/SKILL.md`（5 个示范）。
3. 写出 `outputs/$DAY/artifacts/director_plan.json`（`"version": "6.2"`）。
4. 校验：`$PY -m ai_morning_brief.daily --date $DAY --check-plan`，按报错改，最多 2 次。
5. 重新运行第 1 步的命令。校验仍失败也重新运行——程序会自动改用兜底稿。

## 第 3 步（交接：voice 配音，整段请求）

1. 打开 `outputs/$DAY/artifacts/tts_manifest.json`，详细要求见 `skills/ai-brief-doubao-voice/SKILL.md`。
2. 对 **`todo`** 里每一条（通常只有 3–5 条，每条是几句话连读的一整段，≤300 字），**按顺序一次只发一条**：
   - 调用 `text_to_audio_plus`，把该条 `request` **原样**发送（已填好固定音色描述和原文：`"{音色描述}"。朗读以下文字："{这一段原文}"`）
   - 原文里 GPU、RSA-260、552B 等英文和阿拉伯数字**照原样读**，不拆单字母、不念中文数字、不增删字词；不要拆开或合并段落；不要在请求里要求语速（程序统一加速）
   - 保存到该条 `output_path`
   - 遇到“合成太频繁”等限流报错：**等 10 秒再重试同一条**，最多 3 次；不要并发
3. 全部保存后重新运行第 1 步的命令，程序自动把每段切回单句。若某段没切开，会再交接一次，`todo` 里变成这一段的单句，照做后再运行。
4. 语音工具不可用：改用 `$PY -m ai_morning_brief.daily --date $DAY --min-stories 3 --tts edge` 运行，程序自动配音。

## 第 4 步：完成通知

```
✅ AI每日早报 YYYY-MM-DD 已生成
📹 视频：outputs/日期/renders/ai-daily-news-日期.mp4（1080×1920，XX 秒）
🖼️ 封面：outputs/日期/release-kit/covers/3x4.png（小红书）、9x16.png（TikTok）
📝 文案：outputs/日期/release-kit/publish-package/publish-copy.md（TikTok 标题+正文+话题；小红书标题+正文；含资讯来源）
📦 整合包：outputs/日期/release-kit/publish-package/
🔎 自检：outputs/日期/artifacts/selfcheck/contact-sheet.png
⚠️ 发布前请人工过一遍视频。
```

封面由程序按模板自动生成（3:4、9:16 两张），**不需要交接、不需要生图**。

## 可选：看图自检（不阻塞发布）

打开 `artifacts/selfcheck.json` 的 `contact_sheet` 图片，回答 `agent_checklist.questions` 的 5 个是/否问题，写入 `answers_path`。若有 no，可以只改一次 `director_plan.json` 的文字，然后运行 `$PY -m ai_morning_brief.daily --date $DAY --from director`。

## 失败处理

| 情况 | 处理 |
|---|---|
| `bootstrap` 失败 | 重新运行 `bash scripts/bootstrap_env.sh`；仍失败则报告错误并停止 |
| `fetch` 失败 | AIHOT 暂时不可用：等 5 分钟重跑一次；仍失败报告“AIHOT 暂时不可用” |
| 退出码 4 | 新闻不足 3 条，报告并停止，不补旧闻 |
| `director` 写不好 | 重新运行即可，第 2 次失败自动用兜底稿；或直接加 `--no-agent` |
| `voice` 豆包不可用 | 加 `--tts edge` 重新运行 |
| `render` 失败 | 运行 `--from render` 重试一次；仍失败报告 `run_state.json` 里的 error |
| 想完全不交接 | `$PY -m ai_morning_brief.daily --date $DAY --min-stories 3 --no-agent`（兜底稿 + edge 配音，全自动） |

任何失败都不要删除 `outputs/` 里的中间文件，重跑会自动从断点继续。

## 补跑某天

`$PY -m ai_morning_brief.daily --date 2026-10-30`。历史日期没有实时热点榜和站内搜索，素材会偏少。已成功的日期需要重做时加 `--force`。

## 说明

- 选题会自动参考 AIHOT 日报；日报每天北京时间 08:00 发布，08:00 前运行时只用精选和实时热点。
- 改用声音克隆：加 `--tts-mode voice-clone`（`audio_to_audio_plus` + `example-audio.mp3`）。
- 本 skill 只生成私有成品，**绝不自动上传或发布**。
