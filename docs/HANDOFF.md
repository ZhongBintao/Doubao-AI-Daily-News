# 交接说明 · v6 竖屏版（2026-11-04，模板定稿）

新对话请先读本文件，再读 `AGENTS.md` 与 `docs/design/v6-vertical-template.md`（第 10–12 节是修改历程）。

## 当前状态

- 分支：`design/v6-vertical-template`（**不要合并到 main，不要开 PR**，用户明确要求单独放分支）
- 模板与写稿契约已定稿（2026-11-04 用户确认）；131 个离线测试全部通过
- 参考成片：`--date 2026-11-04 --tts edge`，导演稿 `docs/handoff/2026-11-04-director_plan.json`（A 档标题），2 头条 + 4 快讯，约 129 秒，自检通过
- 历史：10-30（第一期，6.0 契约，44 秒，内容单薄）→ 11-03（6.1 素材包，155 秒，`docs/handoff/2026-11-03-director_plan.json` 已转为 6.2）→ 11-04（6.2 钩子标题）

## 用户已确定的决定

1. 发布平台：国际版 TikTok（面向华人）+ 小红书，竖屏；全自动运行，不需要人工确认；定时时间由用户自己设置
2. 视觉：基于原 v5 模板演进（米白纸张底、白色圆角卡片），对不同类型新闻通用
3. 照顾能力较弱的模型：AI 只做“选择题 + 填空题”，有模板、示范、校验器、兜底
4. **内容只来自 AIHOT 网站本身**（综述、各来源报道摘要、站内搜索、相关事件）；**不抓取、不打开外部原文链接**
5. **内容质量优先**，时长随内容，**上限 3 分钟**
6. **条数灵活**：头条 ≤3、快讯 ≤5；门槛按热度，名额内“热度 + 素材加分”排序，热度相近时素材多的优先
7. **配色方案 A**：按版块分色——头条陶土橙、快讯藏青；类别只作灰色文字标签
8. **背景音乐**：只用仓库内三段（opening / middle-loop / ending），按新版模板编排（见 AGENTS.md）
9. **结尾不显示资讯来源**，来源写进 TikTok / 小红书简介
10. **标题**：大标题与卡片小标题用短视频钩子风格（8 种套路写 3 个挑 1 个），语气按 **A 档**（口语、有画面感、可反问，但事实不变）；卡片不显示栏目标签
11. 用户已取得 AIHOT 书面授权，无需再提醒

## 还没验证 / 可能要处理（都不影响在本环境出片）

- **豆包环境实测**：逐句语音克隆、`bootstrap_env.sh`、以及豆包云电脑上的写稿模型能否稳定写出 6.2 导演稿（3 个标题候选 + 钩子小标题）都没在豆包环境跑过；校验失败 2 次会自动用兜底稿（兜底标题偏保守，用 AIHOT 原标题）
- **语速**：时长预算按 5.5 单位/秒估算（edge 实测约 6.5）；豆包克隆声如果明显更慢，调 `config.SPEECH_CHARS_PER_SECOND`
- **已播记录**存在 `outputs/_state/aired_events.json`，云电脑重置会丢（丢失时用前 3 天日报头条近似重建）
- **补跑 7 天以前的日期**：站内搜索与热点都取不到，素材退回薄素材
- **音乐来源**：`assets/music/README.md` 记录三段曲子取自 Publication Podcast Studio 插件；用户已决定使用仓库内这三段
- 钩子页是否加通用视觉元素（如超大 Logo 背景）仍待定，不影响出片

## 修改过的既有 bug

- 改写某句旁白后重跑，配音会沿用旧音频（`voice._drop_stale_audio`）
- 小红书标题因中英文空格被截断（`publish._compact`）

## 关键文件

| 文件 | 作用 |
|---|---|
| `ai_morning_brief/daily.py` | 编排器（阶段、交接、兜底、CLI） |
| `ai_morning_brief/newsroom.py` + `aihot_v2.py` | 取数、补充素材、合并、选题（权重 `config.py` 的 `HEAT_*`） |
| `ai_morning_brief/material.py` | 素材包、相关性判断、字数预算、时长估算 |
| `ai_morning_brief/director.py` | 导演稿任务、校验器、兜底稿、逐句单元（契约 6.2） |
| `ai_morning_brief/voice.py` / `timeline.py` / `audio_mix.py` | 逐句配音、时间轴、混音 |
| `ai_morning_brief/template/v6/index.html` + `render.py` | 竖屏模板（CSS、预排版、时间轴）与渲染 |
| `ai_morning_brief/selfcheck.py` / `covers.py` / `publish.py` | 自检、封面、发布包 |
| `ai_morning_brief/config.py` 末尾 v6 段 | 所有可调参数（素材包、节目单、预算、时长、停顿、混音） |
| `skills/ai-brief-editorial-writer/` | 写稿 skill 与 5 个示范（改 skill 需同步 `plugins/` 和 `.agents/skills/` 两处副本） |
| `ai_morning_brief/fixtures/aihot_v2/snapshot_2026-11-03_live.json` | 真实实时快照（含综述、来源报道、搜索），测试用 |

## 在 Claude Code 云环境复现

```bash
python3 -m pip install -q numpy Pillow edge-tts
mkdir -p .local/fonts && curl -sSL -o .local/fonts/NotoSansSC-VF.ttf \
  "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
python3 -m unittest discover -s ai_morning_brief/tests          # 应为 131 OK
python3 -m ai_morning_brief.daily --date 2026-11-04 --tts edge  # 停在 director 交接（用当天日期即可）
cp docs/handoff/2026-11-04-director_plan.json outputs/2026-11-04/artifacts/director_plan.json  # 只在同一天、pool 未变时可直接用
python3 -m ai_morning_brief.daily --date 2026-11-04 --check-plan
python3 -m ai_morning_brief.daily --date 2026-11-04 --tts edge  # 跑完全部阶段
```

注意：热点与站内搜索只有当前快照，换一天复现时选题会不同，参考导演稿里的 item_id 可能不在 pool 里而校验失败——那时按任务文件重新写稿即可。网络需允许 `aihot.news`、`speech.platform.bing.com`（edge-tts）、`raw.githubusercontent.com`（字体）。`outputs/` 不在仓库里。

## 历史

- 第一期 10-30 导演稿：`docs/handoff/2026-10-30-director_plan.json`（6.0 契约，已不能通过当前校验，仅作对比）
