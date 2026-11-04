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

## 豆包生产接入（2026-11-04）

- 豆包配音默认改为用户在 `feat/tts-text-to-audio` 上定下的方式：`text_to_audio_plus` + 固定女主播音色描述，读原文（英文、阿拉伯数字照原样），每句统一加速 1.25 倍（音高不变）；声音克隆保留为 `--tts-mode voice-clone`
- `--min-stories 3`：可播新闻少于 3 条时干净终止（退出码 4），不补旧闻
- 豆包提示词改为 v6 流程：拉 `design/v6-vertical-template` 分支、不装 faster-whisper、交接点只有 director 与 voice、封面自动生成（3:4 / 9:16，无 16:9）、视频 1080×1920（见 `entry-skill/ai-daily-news-studio/SKILL.md`）
- 本环境用 edge 声音模拟豆包交接跑通全流程（123 秒，自检通过）；真实豆包仍未实测
- 豆包首次实跑被限流（“合成太频繁”）→ 改为**整段请求**：相邻几句合成一段（≤300 字，整条新闻优先，十几字的结尾并入前段），一期 26–27 句从 26–27 次请求降到 3–4 次；程序在停顿处切回单句（`voice.split_batch`），切不开的段只对该段退回逐句请求；克隆模式同样适用。用 edge 两种声音、多种语速整段合成 11-03 / 11-04 两期验证，切点全部落在真实句间停顿（以 edge 逐词时间为准）。交接提示要求一次一条、限流等 10 秒重试

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
