# AI每日早报 · AI Daily News（v6 竖屏版）

每天从 [AIHOT](https://aihot.news) 选出真正的重磅 AI 资讯，自动生成一条 **1080×1920 的中文竖屏资讯视频**（时长随内容决定，上限 3 分钟）（TikTok / 小红书），附 3:4 与 9:16 封面和两平台发布文案。

- 开头 3 秒钩子：第一帧就是头条主角与一句抓人的话
- 重磅选题：AIHOT 精选评分 + 日报 + 实时多源热点 + 事件综述 → 站内补充素材 → 重磅分 → 同事件合并 → 已播去重 → 头条 ≤3 条 + 快讯 ≤5 条（按当天内容决定）
- 照顾能力较弱的模型：AI 只做“选择题 + 填空题”，有示范案例和逐字段中文报错；每一步都有自动兜底，保证每天出片
- 逐句配音：豆包语音克隆或 edge-tts；画面、卡片高亮、字幕按句精确对齐
- 混音：音乐低于人声约 18 dB，说话时自动再压低；−16 LUFS
- 设计记录：[docs/design/v6-vertical-template.md](docs/design/v6-vertical-template.md)

## 运行

```bash
bash scripts/bootstrap_env.sh && source .local/env.sh
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date 2026-10-30            # 运行 / 断点续跑
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date 2026-10-30 --no-agent # 全自动（兜底稿 + edge 配音）
OpenMontage/.venv/bin/python -m ai_morning_brief.daily --date 2026-10-30 --status
```

豆包云电脑每日自动运行见 [快速开始.md](快速开始.md)、[豆包运行手册.md](豆包运行手册.md) 与入口 skill [entry-skill/ai-daily-news-studio](entry-skill/ai-daily-news-studio/SKILL.md)。项目契约见 [AGENTS.md](AGENTS.md)。

## 代码结构（v6）

| 模块 | 职责 |
|---|---|
| `ai_morning_brief/daily.py` | 10 阶段编排器，断点续跑、Agent 交接、自动兜底 |
| `aihot_v2.py` | AIHOT 2.0 JSON API 客户端（精选、日报、热点、事件） |
| `newsroom.py` | 期号窗口、重磅分、同事件聚类、已播记录、节目单 |
| `director.py` | 导演稿任务、校验器（中文报错）、兜底稿、逐句单元 |
| `voice.py` | 逐句配音（豆包交接 / edge-tts）、静音裁剪、对齐 |
| `timeline.py` | 逐句时间轴、场景/卡片事件、字幕切分 |
| `audio_mix.py` | 音乐分段对齐、侧链闪避、两遍 loudnorm |
| `render.py` + `template/v6/index.html` | HyperFrames 工作区、Chrome 预排版、渲染与校验 |
| `selfcheck.py` / `covers.py` / `publish.py` | 成片自检、HTML 封面、发布文案与整合包 |

测试：`python3 -m unittest discover -s ai_morning_brief/tests -v`（全部离线）。

旧版 1920×1080 横屏流水线（`python -m ai_morning_brief.pipeline`）仅作历史兼容。

## 许可

代码见 [LICENSE](LICENSE)。字体 Noto Sans SC 为 SIL OFL 授权，运行时下载到 `.local/fonts/`。AIHOT 数据与第三方原文版权归原作者，使用须遵守 [AIHOT 使用规则](https://aihot.news/terms)。
