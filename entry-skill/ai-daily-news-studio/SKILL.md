---
name: ai-daily-news-studio
description: 每日自动生成 AI 每日早报视频（1920×1080 MP4，含人声、背景音乐、字幕、封面）。触发词：AI 每日早报、AI早报、生成早报、每日新闻视频、今天的早报。豆包驱动，零 API 密钥，自动检查并安装环境。
metadata:
  author: ZhongBintao
  version: "1.0.0"
---

# AI 每日早报 · 豆包自动化

从 AIHOT 精选 24 小时资讯，由豆包完成写稿、语音合成、封面生成，OpenMontage 渲染视频。零 API 密钥，全部使用豆包产品内功能。

## 使用方式

用户只需说「生成今天的 AI 每日早报」。

本 skill 已自带仓库地址、克隆方式、环境自检、完整流程与交付物约定，
**不需要用户在提示词里重复任何细节**。定时任务里写那一句话就够了；
所有约定改在仓库里，云电脑下次运行 `git pull` 自动生效。

## 项目位置

标准安装路径：`/home/user/.super_doubao/super-doubao-runtime/workspace/ai-daily-news-studio-codex-plugin/`

GitHub 仓库：`https://github.com/ZhongBintao/Doubao-AI-Daily-News`

## 第一步：环境检查与自动安装

云电脑运行一段时间后会被重置，**每次触发都必须重跑自检**，不要假设环境还在。
自检是幂等的：已具备的依赖跳过，缺失的自动补装到项目内 `.local/`，全程不需要 root。

```bash
PROJECT_DIR="/home/user/.super_doubao/super-doubao-runtime/workspace/ai-daily-news-studio-codex-plugin"

# 1. 项目目录：不存在则克隆，已存在则拉最新代码
if [ ! -d "$PROJECT_DIR/.git" ]; then
  git clone https://github.com/ZhongBintao/Doubao-AI-Daily-News.git "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
git pull --ff-only || echo "拉取最新代码失败，继续使用本地版本"

# 2. 环境自检：Python venv + Node >= 22 + ffmpeg/ffprobe + .env
bash scripts/bootstrap_env.sh || exit 1

# 3. 注入 PATH（ffmpeg/node 装在项目内，不注入则子进程找不到）
source .local/env.sh
```

自检脚本自动完成：创建虚拟环境并装依赖、按需下载 Node.js 22 LTS 与静态
ffmpeg/ffprobe（官方源失败时自动回退国内镜像）、从 `.env.example` 生成 `.env`。

**两个易错点**：
- 不要执行 `npm install -g hyperframes`。渲染走 `npx hyperframes`，CLI 首次渲染时自动拉取；
  全局安装在无 root 环境反而会失败。
- 后续**每一条** pipeline 命令前都要有 `source .local/env.sh`，否则 ffmpeg 不在 PATH 上，
  会在混音阶段报 `ffmpeg not found`。

输出 `BOOTSTRAP_OK` 后，进入日常运行流程。

## 固定配置（不需要用户每次说明）

| 项目 | 配置 |
|------|------|
| TTS | 豆包对话内语音合成（`text_to_audio_plus`），活泼快语速女声 |
| 音色描述 | 年轻女性，声音活泼甜美，充满活力，像晨间电台元气女主播，语速偏快，咬字清晰有节奏感，情绪明亮有感染力，无背景音无杂音，纯人声 |
| 字幕 | 比例估算（豆包 TTS 无字级时间戳），文字 100% 来自写稿文案 |
| 封面 | Seedream `image_edit` + 双参考图（理想效果图 + 风格系统图）+ 中文防护罩 prompt，文字准确性校验重试 1 次 |
| 截图 | 关闭（`--source-visual-mode off`），视频统一用资讯卡片 |
| API 密钥 | 不需要，全部使用豆包产品内功能 |
| 视频规格 | 1920×1080 MP4，含人声 + 背景音乐 + 字幕 |

## 默认交付物（每次都要，不要询问用户是否需要）

1. **视频** — `outputs/YYYY-MM-DD/renders/ai-daily-news-YYYY-MM-DD.mp4`（1920×1080）
2. **封面** — 16:9 / 3:4 / 9:16 三张，位于 `release-kit/covers/`
3. **发布文案整合包** — `outputs/YYYY-MM-DD/release-kit/`，
   标题固定 `AI每日早报YYYY-MM-DD`，文案基于冻结素材撰写

三者都是默认交付物，用户没提也要做。某项缺失必须在完成通知里写明原因，不能含糊带过。

## 日常运行流程（8 步）

详细操作见项目内 `豆包运行手册.md`。简要流程：

1. **环境确认**（已在上方完成）
2. **冻结素材**：`pipeline prepare --source-visual-mode off`，从 AIHOT 抓取 24h 精选资讯，选中 6-8 条
3. **编辑写稿**（豆包完成）：读取 editorial_input.json，按 v5 规范生成 editorial_plan.json，通过质量校验
4. **生成旁白脚本**：基于 editorial_plan 生成 narration_plan.json
5. **豆包 TTS 合成**（豆包完成）：
   - `doubao_tts_adapter prepare` 生成待合成清单
   - 逐段调用 `text_to_audio_plus`，使用上方固定音色描述
   - `doubao_tts_adapter finalize` 归一化音频、自动填充短时长、生成 manifest
6. **视频渲染**：`pipeline run --reuse-audio --speech-provider doubao`，自动完成字幕估算、混音、HyperFrames 渲染、质量门禁
7. **封面生成**（默认）：按 `skills/ai-brief-cover-generator-doubao/SKILL.md` 执行，用 `image_edit` 生成 16:9/3:4/9:16 三种封面
8. **发布文案整合包**（默认）：按 `skills/ai-brief-release-kit/SKILL.md` 执行，
   `release_workflow.py prepare` 冻结文案 → `finalize` 把视频、封面、文案组装成发布包

完成后按「默认交付物」三项汇总告知用户：视频（路径/时长/大小）、三张封面、发布文案包。

## 关键文件路径

| 文件 | 路径 |
|------|------|
| 运行手册 | `项目/豆包运行手册.md` |
| 语音规范 | `项目/skills/ai-brief-doubao-voice/SKILL.md` |
| 封面规范 | `项目/skills/ai-brief-cover-generator-doubao/SKILL.md` |
| 发布规范 | `项目/skills/ai-brief-release-kit/SKILL.md` |
| 发布计划 | `项目/outputs/YYYY-MM-DD/release-kit/release_plan.json` |
| 发布整合包 | `项目/outputs/YYYY-MM-DD/release-kit/` |
| TTS 适配器 | `项目/ai_morning_brief/doubao_tts_adapter.py` |
| 每日产出 | `项目/outputs/YYYY-MM-DD/` |
| 最终视频 | `项目/outputs/YYYY-MM-DD/renders/ai-daily-news-YYYY-MM-DD.mp4` |

## 失败处理

- 选中资讯不足 3 条 → 终止，告知用户今日素材不足
- 写稿校验 2 次仍失败 → 终止，保存错误供人工排查
- 某段 TTS 合成失败 → 重试 1 次，仍失败则终止
- 视频渲染失败 → 查看 run_report.json 的 failed_stage，音频已生成可用 `--reuse-audio` 重跑
- 封面生成失败 → 不影响主视频，记录后跳过

任何阶段失败都不删除已生成的中间产物，可用于补跑。

## 补跑

用户说"补跑 YYYY-MM-DD 的早报"时：
- 当天已成功 → 询问是否 `--force` 重新生成
- 部分产物存在 → 从失败阶段继续，音频已生成用 `--reuse-audio`，素材已冻结用 `--reuse-source`
