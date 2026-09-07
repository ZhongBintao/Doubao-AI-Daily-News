---
name: ai-daily-news-studio
description: 每日自动生成 AI 每日早报视频（1920×1080 MP4，含语音克隆人声、背景音乐、字幕、3种尺寸封面、发布文案）。支持 AIHOT 稀疏日的七天未播回退和无新闻状态短报。触发词：AI 每日早报、AI早报、生成早报、每日新闻视频、今天的早报。云电脑原生，单入口编排，自动断点续跑，语音克隆音色。
metadata:
  author: ZhongBintao
  version: "2.0.0"
---

# AI 每日早报 · 云电脑原生自动化

从 AIHOT 精选 24 小时资讯，由豆包完成写稿、语音克隆合成、封面生成，OpenMontage 渲染视频。24 小时精选为空或无可选条目时，自动审计查询最近 7 天，排除前 7 个通过版本已经播过的 item ID，最多补入 3 条未播资讯；如果仍然没有未播资讯，则生成明确的 `no-news` 状态短报，不补旧闻或占位内容。单入口编排器自动调度全流程，断点续跑，适合云电脑定时无人值守运行。

## 使用方式

用户只需说「生成今天的 AI 每日早报」。

本 skill 已自带仓库地址、克隆方式、环境自检、完整流程与交付物约定，
**不需要用户在提示词里重复任何细节**。定时任务里写那一句话就够了；
所有约定改在仓库里，云电脑下次运行 `git pull` 自动生效。

## 项目位置

标准安装路径：`/home/user/.super_doubao/super-doubao-runtime/workspace/ai-daily-news-studio-codex-plugin/`

GitHub 仓库：`https://github.com/ZhongBintao/Doubao-AI-Daily-News`

## 快速运行（推荐）

### 第 0 步：环境准备（每次运行前，约 10 秒；重置后首次约 3-8 分钟）

```bash
PROJECT_DIR="/home/user/.super_doubao/super-doubao-runtime/workspace/ai-daily-news-studio-codex-plugin"

# 项目目录：不存在则克隆，已存在则拉最新代码
if [ ! -d "$PROJECT_DIR/.git" ]; then
  git clone https://github.com/ZhongBintao/Doubao-AI-Daily-News.git "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
git pull --ff-only || echo "拉取最新代码失败，继续使用本地版本"

# 幂等环境自检（venv / Node>=22 / ffmpeg / .env / 参考音频 / 磁盘空间）
bash scripts/bootstrap_env.sh || exit 1

# 注入 PATH（ffmpeg/node 装在项目内，不注入则子进程找不到）
source .local/env.sh
```

输出 `BOOTSTRAP_OK` 后继续。

### 第 1 步：运行编排器

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date $(date +%Y-%m-%d)
```

编排器自动执行 9 个阶段，遇到需要 Agent（LLM）完成的阶段时打印交接指令并退出。
你（Agent）完成交接工作后，**重新运行同一命令**即可自动从断点继续。

### 三个 Agent 交接点

全流程只有 3 个需要你（Agent）动手的阶段，其余全部自动：

#### 交接点 1：编辑写稿（editorial）

编排器生成 `outputs/YYYY-MM-DD/artifacts/editorial_task.json`（包含冻结资讯、写稿请求、草稿、校验标准）。

你需要：
1. 读取 `editorial_task.json`
2. 按 v5 规范写入 `outputs/YYYY-MM-DD/artifacts/editorial_plan.json`
3. 运行任务文件中的校验命令，确保 `editorial_quality_report.json` 的 `status = pass`
4. 重新运行编排器

#### 交接点 2：语音克隆合成（voice_clone）

编排器生成 `outputs/YYYY-MM-DD/artifacts/tts_manifest.json`（包含每段的 spoken_text 和保存路径）。

你需要：
1. 读取 `tts_manifest.json`
2. 对每一段调用 `audio_to_audio_plus`：
   - `@音频1` = 项目根目录的 `example-audio.mp3`（语音克隆参考）
   - prompt：`"用参考音频的音色、语速和朗读风格，清晰朗读以下文字，不增删字词，无背景音无杂音：{spoken_text}"`
3. 将返回的音频保存到清单中指定的 `output_path`
4. 全部完成后重新运行编排器

如果 `editorial_input.json` 的 `selection.mode` 是 `no-news`，编排器已经
生成确定性的零讯状态计划，不需要补写新闻故事；继续运行即可进入豆包语音
克隆和视频渲染。该版本会跳过来源绑定封面和发布整合包，但仍然生成并校验
视频。

**注意**：必须严格使用 spoken_text 原文，不增删字词；每段单独合成。

#### 交接点 3：封面生成（cover）

编排器生成 `outputs/YYYY-MM-DD/release-kit/covers/cover_task.json`（包含三种尺寸的 prompt 和参考图路径）。

你需要：
1. 读取 `cover_task.json`
2. 使用 `image_edit` 工具（不是 image_gen），参考 `cover-style-system-16x9.png`
3. 按顺序生成 16:9 (1920×1080) → 3:4 (1080×1440) → 9:16 (1080×1920)
4. 保存到 `outputs/YYYY-MM-DD/release-kit/covers/` 下对应文件名
5. 重新运行编排器

### 完成

编排器执行完所有阶段后，打印最终产出汇总。

## 固定配置

| 项目 | 配置 |
|------|------|
| TTS | 豆包语音克隆（`audio_to_audio_plus` + `example-audio.mp3`） |
| 参考音频 | 项目根目录 `example-audio.mp3`（29.8秒，随仓库分发） |
| 字幕 | 比例估算（豆包 TTS 无字级时间戳），文字 100% 来自写稿文案 |
| 封面 | `image_edit` + 风格系统参考图，三种尺寸（16:9 / 3:4 / 9:16） |
| 截图 | 关闭（`--source-visual-mode off`），视频统一用资讯卡片 |
| API 密钥 | 不需要，全部使用豆包产品内功能 |
| 视频规格 | 1920×1080 MP4，含人声 + 背景音乐 + 字幕 |
| 编排器 | `ai_morning_brief.daily`，单入口，自动断点续跑 |

## 默认交付物（有新闻时）

1. **视频** — `outputs/YYYY-MM-DD/renders/ai-daily-news-YYYY-MM-DD.mp4`（1920×1080）
2. **封面** — 16:9 / 3:4 / 9:16 三张，位于 `release-kit/covers/`
3. **发布文案整合包** — `outputs/YYYY-MM-DD/release-kit/`，标题固定 `AI每日早报YYYY-MM-DD`

有新闻时三者都是默认交付物，用户没提也要做。`no-news` 状态短报仍必须
生成并通过质量门禁的视频，但会明确跳过来源绑定封面和发布整合包。

## 编排器命令参考

```bash
# 完整运行（自动断点续跑）
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date YYYY-MM-DD

# 查看当前进度（不执行）
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date YYYY-MM-DD --status

# 强制从头重跑
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date YYYY-MM-DD --force

# 跳过封面和发布（只出视频）
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date YYYY-MM-DD --skip cover,release

# 从渲染阶段重跑（音频已生成）
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date YYYY-MM-DD --from render
```

## 失败处理

| 失败阶段 | 处理方式 |
|---------|---------|
| 24 小时精选为空或无可选条目 | 自动查询 selected `window=7d`，排除前 7 个通过版本已播 item ID，最多选 3 条未播资讯 |
| 七天回退仍无未播资讯 | 生成 `edition_mode=no-news` 状态短报，不使用旧闻、虚构内容或占位内容；跳过来源绑定封面和发布包 |
| 写稿校验 2 次仍失败 | 终止，保存错误供人工排查 |
| 某段语音合成失败 | 重试 1 次，仍失败则记录；编排器检测到缺失会停在 voice_clone 阶段 |
| 视频渲染失败 | 查看 `run_report.json` 的 `failed_stage`，修复后重新运行编排器（音频已生成会自动复用） |
| 封面生成失败 | 不影响主视频，记录后跳过（发布包会在无封面时降级） |

任何阶段失败都不删除已生成的中间产物，可用于补跑。编排器的 `run_state.json` 记录了每个阶段的状态和错误。

## 补跑

用户说"补跑 YYYY-MM-DD 的早报"时：
- 当天已成功 → 询问是否 `--force` 重新生成
- 部分产物存在 → 直接运行编排器，自动从断点继续
- 音频已生成但渲染失败 → 编排器自动复用音频，从 render 阶段继续
- 素材已冻结 → 编排器自动复用源数据

## 关键文件路径

| 文件 | 路径 |
|------|------|
| 编排器入口 | `python -m ai_morning_brief.daily` |
| 运行状态 | `outputs/YYYY-MM-DD/run_state.json` |
| 运行手册 | `项目/豆包运行手册.md` |
| 语音规范 | `项目/skills/ai-brief-doubao-voice/SKILL.md` |
| 封面规范 | `项目/skills/ai-brief-cover-generator-doubao/SKILL.md` |
| 发布规范 | `项目/skills/ai-brief-release-kit/SKILL.md` |
| TTS 适配器 | `项目/ai_morning_brief/doubao_tts_adapter.py` |
| 参考音频 | `项目/example-audio.mp3` |
| 最终视频 | `outputs/YYYY-MM-DD/renders/ai-daily-news-YYYY-MM-DD.mp4` |
| 每日产出 | `outputs/YYYY-MM-DD/` |

## 手动分步模式（调试用）

如果编排器出现问题需要手动调试，原 8 步手动流程仍然可用，详见 `豆包运行手册.md` 的"手动分步运行"附录。手动模式下 TTS 使用 `doubao_tts_adapter.py` 的 prepare/status/finalize 三个子命令。
