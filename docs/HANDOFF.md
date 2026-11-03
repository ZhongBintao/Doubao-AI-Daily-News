# 交接说明 · v6 竖屏版（2026-11-03）

新对话请先读本文件，再读 `AGENTS.md` 与 `docs/design/v6-vertical-template.md`。

## 当前状态

- 分支：`design/v6-vertical-template`（**不要合并到 main，不要开 PR**，用户明确要求单独放分支）
- 已完成：v6 竖屏流水线（1080×1920，TikTok / 小红书），10 阶段编排器 `python -m ai_morning_brief.daily`，104 个离线测试全部通过
- 已实跑一期：`--date 2026-10-30 --tts edge`，导演稿由 Agent 手写（见下方“复现”），成片 44 秒，自检 pass
- 用户已下载成片，**视频还有问题需要修改**（具体问题待用户在新对话中说明）

## 用户已确定的决定

1. 发布平台：TikTok + 小红书，竖屏，时长不能太长；全自动运行，不需要人工确认；定时时间由用户自己设置
2. 视觉：基于原 v5 模板演进（米白纸张底、陶土橙、深青、白色圆角卡片），不要另起风格；必须对不同类型新闻通用
3. 必须照顾能力较弱的模型：AI 只做“选择题 + 填空题”，有模板、示范、校验器、兜底
4. 选题：从 AIHOT 选真正的重磅新闻（重磅分 + 同事件合并 + 已播去重），每期 1 头条 + 最多 3 快讯
5. 用户已取得 AIHOT 书面授权，无需再提醒

## 暂未决定 / 待办

- **视频问题修改**（用户下一步要提）
- **背景音乐**：仍是 `ai_morning_brief/assets/music/` 里三段旧曲（节奏慢、TikTok 商用许可未确认），用户说“待会再说”
- 已播记录存储位置：现在在 `outputs/_state/aired_events.json`（云电脑重置会丢，丢失时用前 3 天日报头条近似重建）
- 豆包逐句语音克隆未实测（本环境无豆包工具，实跑用 edge-tts `zh-CN-YunxiNeural`）
- `scripts/bootstrap_env.sh` 的改动（字体下载、`hyperframes browser ensure`、edge-tts）未在豆包云电脑上验证
- 钩子页是否加通用视觉元素（超大 Logo 背景等）仍待定
- 我替用户做的默认选择（可推翻）：去掉概览页、保留 2.6 秒关注结尾页；封面改为 HTML 模板（不再用 AI 生图）

## 关键文件

| 文件 | 作用 |
|---|---|
| `ai_morning_brief/daily.py` | 编排器（阶段、交接、兜底、CLI） |
| `ai_morning_brief/newsroom.py` + `aihot_v2.py` | 取数与选题（权重在 `config.py` 的 `HEAT_*`） |
| `ai_morning_brief/director.py` | 导演稿任务、校验器、兜底稿、逐句单元 |
| `ai_morning_brief/voice.py` / `timeline.py` / `audio_mix.py` | 逐句配音、时间轴、混音 |
| `ai_morning_brief/template/v6/index.html` + `render.py` | 竖屏模板（CSS、预排版脚本、时间轴脚本）与渲染 |
| `ai_morning_brief/selfcheck.py` / `covers.py` / `publish.py` | 自检、封面、发布包 |
| `ai_morning_brief/config.py` 末尾 v6 段 | 所有可调参数（预算、时长、停顿、混音、阈值） |
| `skills/ai-brief-editorial-writer/` | 写稿 skill 与 4 个示范（改 skill 需同步 `plugins/` 和 `.agents/skills/` 两处副本） |

## 在 Claude Code 云环境复现

```bash
# 依赖：ffmpeg、node≥22/npx、Chrome（系统自带 /usr/bin/google-chrome）
python3 -m pip install -q numpy Pillow edge-tts
# 字体（约 17 MB，下载到 .local/fonts/，已被 gitignore）
mkdir -p .local/fonts && curl -sSL -o .local/fonts/NotoSansSC-VF.ttf \
  "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
python3 -m unittest discover -s ai_morning_brief/tests          # 应为 104 OK
python3 -m ai_morning_brief.daily --date 2026-10-30 --tts edge  # 停在 director 交接
# 写 outputs/2026-10-30/artifacts/director_plan.json（见下），然后：
python3 -m ai_morning_brief.daily --date 2026-10-30 --check-plan
python3 -m ai_morning_brief.daily --date 2026-10-30 --tts edge  # 跑完全部阶段
python3 -m ai_morning_brief.daily --date 2026-10-30 --tts edge --from render  # 只改了模板时
```

环境注意：网络需要允许 `aihot.news`、`speech.platform.bing.com`（edge-tts）、`raw.githubusercontent.com`（字体）。edge-tts 自动读取 `HTTPS_PROXY` 与 `SSL_CERT_FILE`。`outputs/` 不在仓库里，新环境需重新跑。

## 2026-10-30 实跑用的导演稿

已存为 `docs/handoff/2026-10-30-director_plan.json`，复现时复制到 `outputs/2026-10-30/artifacts/director_plan.json` 即可（item_id 来自 AIHOT，期号选题结果确定时可直接通过校验）。
