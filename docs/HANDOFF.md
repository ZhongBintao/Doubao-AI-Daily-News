# 交接说明 · v6 竖屏版（2026-11-04，模板定稿 + 豆包长录音配音）

新对话请先读本文件，再读 `AGENTS.md` 与 `docs/design/v6-vertical-template.md`（第 10–13 节是修改历程）。

## 当前状态

- 分支：`design/v6-vertical-template`（**不要合并到 main，不要开 PR**，用户明确要求单独放分支）
- 模板与写稿契约已定稿（2026-11-04 用户确认）；豆包配音改为几段长录音（同日下午，见下文）；152 个离线测试全部通过
- 豆包配音已用 2 段真实豆包音频验证去水印和切句（全部正确），整条流程已在本环境用 edge 模拟豆包长录音跑通；**下一步：同事在豆包云电脑上实跑一期**（检查清单见下一节）
- 参考成片：`--date 2026-11-04 --tts edge`，导演稿 `docs/handoff/2026-11-04-director_plan.json`（A 档标题），2 头条 + 4 快讯，约 129 秒，自检通过
- 历史：10-30（第一期，6.0 契约，44 秒，内容单薄）→ 11-03（6.1 素材包，155 秒，`docs/handoff/2026-11-03-director_plan.json` 已转为 6.2）→ 11-04（6.2 钩子标题）

## 豆包实跑检查清单（给同事）

**怎么跑**：在豆包云电脑上照 `entry-skill/ai-daily-news-studio/SKILL.md` 做（第 0 步会拉 `design/v6-vertical-template` 并运行 `bootstrap_env.sh`，必须看到 `BOOTSTRAP_OK`）。交接点只有两个：

1. **director**（写稿）：照 `skills/ai-brief-editorial-writer/SKILL.md` 写 `director_plan.json`，`--check-plan` 校验。写不好也没关系，第 2 次失败自动用兜底稿。
2. **voice**（配音）：照 `skills/ai-brief-doubao-voice/SKILL.md`。`tts_manifest.json` 的 `todo` 通常只有 **1–3 条**，每条是几条新闻连读的一整段（约 1–2 分钟）：
   - 每条调用一次 `text_to_audio_plus`，`request` **原样**发送（已含动漫少女音色描述和原文），不要加语速要求，不要拆开或合并；
   - 返回的音频**原样**保存到该条 `output_path`（如 `assets/audio/raw/t01.wav`），开头的“内容由豆包AI生成”不用处理；
   - 一次只发一条；遇到“合成太频繁”等 10 秒重试同一条，最多 3 次；
   - 存好后重新运行同一条命令。若某段切不开或没读完，会再交接一次，`todo` 里出现 `t01-1`、`t01-2` 这样的半段，照做即可。

**重点看什么**（跑完后在 `outputs/日期/` 里）：

| 看哪里 | 正常的样子 | 不正常时 |
|---|---|---|
| `artifacts/tts_manifest.json` → `takes[].watermark` | 每段 `status: cut`，`pause_start_seconds` ≈ 2.25 | `not-found`：把那段原始音频发回来 |
| `takes[].split` | `status: ok`，`speed_ratios` 都在 0.55–1.8 之间 | `uncut` / `truncated` / `too-short`：会自动拆半重请求；记下发生在哪一段 |
| `takes[].raw_seconds` | 每段 ≤ 120 秒（目标约 100 秒） | 接近 120 秒或豆包拒绝长文本：设 `AI_DAILY_DOUBAO_TAKE_SECONDS=60` 重跑，段会变短变多 |
| `artifacts/selfcheck.json` | `status: pass`，含 `doubao_watermark` 一项 | 把 `checks` 里失败的项发回来 |
| 成片 `renders/ai-daily-news-日期.mp4` | 开头没有“内容由豆包AI生成”；句与句之间节奏自然；换条有停顿；字幕、卡片高亮跟着声音走 | 记下时间点和现象 |

**请发回**：`artifacts/tts_manifest.json`、`artifacts/voice_manifest.json`、`artifacts/selfcheck.json`、`run_state.json`、`outputs/_state/doubao_speech_rate.json`、`artifacts/selfcheck/contact-sheet.png`；有问题的段再附 `assets/audio/raw/` 里对应的原始音频（`t01.wav` 等）。单独检查某个原始音频的水印：`python3 scripts/check_doubao_watermark.py assets/audio/raw/t01.wav`。

**已知要留意**：豆包对 100 秒左右长文本的稳定性、新闻之间的换行会不会让停顿变长（不影响切句，只影响节奏）、第一次运行没有实测语速记录（用默认 4.5，偏保守，段数可能比实际需要多一段）。

## 用户已确定的决定

1. 发布平台：国际版 TikTok（面向华人）+ 小红书，竖屏；全自动运行，不需要人工确认；定时时间由用户自己设置
2. 视觉：基于原 v5 模板演进（米白纸张底、白色圆角卡片），对不同类型新闻通用
3. 照顾能力较弱的模型：AI 只做“选择题 + 填空题”，有模板、示范、校验器、兜底
4. **内容只来自 AIHOT 网站本身**（综述、各来源报道摘要、站内搜索、相关事件）；**不抓取、不打开外部原文链接**
5. **内容质量优先**，时长随内容，**不设总时长上限**（2026-11-04 取消 3 分钟上限；头条 ≤3、快讯 ≤5 和每条预算决定长度）
6. **条数灵活**：头条 ≤3、快讯 ≤5；门槛按热度，名额内“热度 + 素材加分”排序，热度相近时素材多的优先
7. **配色方案 A**：按版块分色——头条陶土橙、快讯藏青；类别只作灰色文字标签
8. **背景音乐**：只用仓库内三段（opening / middle-loop / ending），按新版模板编排（见 AGENTS.md）
9. **结尾不显示资讯来源**，来源写进 TikTok / 小红书简介
10. **标题**：大标题与卡片小标题用短视频钩子风格（8 种套路写 3 个挑 1 个），语气按 **A 档**（口语、有画面感、可反问，但事实不变）；卡片不显示栏目标签
11. 用户已取得 AIHOT 书面授权，无需再提醒
12. **配音音色**：豆包 `text_to_audio_plus` + 动漫少女音色描述，**自然语速，不做任何变速**（不每句加速，也不整片变速）；**取消声音克隆模式**
13. **豆包长录音**：豆包一次最长约 120 秒，长录音音色更稳，所以整期尽量一次或几次合成（每段约 100 秒，上限 120 秒），再去水印、切回单句
14. **去水印**：豆包每条音频开头都有“内容由豆包AI生成”，用户已向 TikTok / 字节跳动客服确认可以去掉（TikTok 发布时有单独的 AI 审查与标注），无需再提醒合规问题

## 还没验证 / 可能要处理（都不影响在本环境出片）

- **豆包环境实测**：去水印和切句已用 2 段真实豆包音频验证；但整期长录音交接、`bootstrap_env.sh`、以及豆包云电脑上的写稿模型能否稳定写出 6.2 导演稿（3 个标题候选 + 钩子小标题）还没在豆包环境完整跑过（见上面的检查清单）；写稿校验失败 2 次会自动用兜底稿（兜底标题偏保守，用 AIHOT 原标题；兜底文字偶尔会在长句的 42 字处硬切）
- **语速**：写稿预算按 5.5 单位/秒估算（edge 约 6.5，豆包自然语速约 5.7）；豆包分段用实测语速（`outputs/_state/doubao_speech_rate.json`，云电脑重置会丢，丢了就回到默认 4.5）
- **已播记录**存在 `outputs/_state/aired_events.json`，云电脑重置会丢（丢失时用前 3 天日报头条近似重建）
- **补跑 7 天以前的日期**：站内搜索与热点都取不到，素材退回薄素材
- **音乐来源**：`assets/music/README.md` 记录三段曲子取自 Publication Podcast Studio 插件；用户已决定使用仓库内这三段
- 钩子页是否加通用视觉元素（如超大 Logo 背景）仍待定，不影响出片

## 豆包生产接入（2026-11-04）

- （已被下午的“长录音配音”取代）豆包配音默认改为 `text_to_audio_plus` + 固定女主播音色描述，每句统一加速 1.25 倍；声音克隆保留为 `--tts-mode voice-clone`
- `--min-stories 3`：可播新闻少于 3 条时干净终止（退出码 4），不补旧闻
- 豆包提示词改为 v6 流程：拉 `design/v6-vertical-template` 分支、不装 faster-whisper、交接点只有 director 与 voice、封面自动生成（3:4 / 9:16，无 16:9）、视频 1080×1920（见 `entry-skill/ai-daily-news-studio/SKILL.md`）
- 本环境用 edge 声音模拟豆包交接跑通全流程（123 秒，自检通过）；真实豆包仍未实测
- 豆包首次实跑被限流（“合成太频繁”）→ 改为**整段请求**：相邻几句合成一段（≤300 字，整条新闻优先，十几字的结尾并入前段），一期 26–27 句从 26–27 次请求降到 3–4 次；程序在停顿处切回单句（`voice.split_batch`），切不开的段只对该段退回逐句请求；克隆模式同样适用。用 edge 两种声音、多种语速整段合成 11-03 / 11-04 两期验证，切点全部落在真实句间停顿（以 edge 逐词时间为准）。交接提示要求一次一条、限流等 10 秒重试
- 豆包允许长音频 → 默认**整期一次请求**（1 次），切不开时退为 ≤300 字小段，再退逐句。切句改为按“每句长度是否合理”做动态规划（不再按绝对位置，长录音不会累积漂移）；edge 7 种声音/语速 × 11-03、11-04 整期合成共 357 个句界全部切对，300 字分段模式 322 个也全部切对

## 豆包长录音配音（2026-11-04 下午，取代上面的整期/小段请求）

- 起因：同事 10 个 `feat/fix(voice)` 提交（按版块分组、去水印、整片变速）是从本地另一分支逐文件覆盖上来的，带来几处问题：封面阶段 NameError；`test_daily.py` 被换成另一套代码的版本（导入不存在的 `hot_rank`，测试从 131 降到 81）；去水印对所有音频生效、会删掉正常句子开头；声音克隆退回逐句请求。已全部修复，测试恢复并补齐。
- 用户给的一期豆包成片（219.6 秒，逐句请求）：语音识别找到 39 处“内容由豆包AI生成”（每处约 1.8 秒 + 前后静音，合计浪费约 100 秒），正文自然语速约 5.7 字/秒。
- **分段**（`voice.plan_takes`）：只在新闻之间切；钩子随第一条头条，结尾随最后一条；段数 = 估算 ÷ 100 秒向上取整，均衡分配（最长段尽量短，不出现“100 + 20”）；每段（估算 + 水印 2.7 秒）× 1.15 ≤ 120 秒；单条新闻超长才在句间拆。段内新闻之间加换行。11-03 参考稿 → 2 段；11-04 实时兜底稿 → 2 段（约 100 秒 + 72 秒）。
- **去水印**（`voice.watermark_scan`）：只对豆包。采用同事在真实音频上总结的规则——10 ms 帧 RMS ≥300 为有声，第一个有声帧之后第一段 ≥0.12 秒（原为 0.18 秒，见下条实测）的静音结束处为正文开头；另加安全检查：这段静音须起于 1.6–3.0 秒，否则不切，自检 `doubao_watermark` 警告。没有水印的音频（如 edge）不会被误切（有测试）。`scripts/check_doubao_watermark.py` 可直接检查真实音频。
- **真实豆包音频实测**（用户提供 2 段，75.3 s / 44.3 s，44.1 kHz 立体声）：标签是同一段固定录音（内部停顿最长 0.08 s），两段都在 2.25 s 结束，其后静音 0.27 / 0.20 s → 门槛由 0.18 s 降到 0.12 s；去水印后识别开头即正文第一句。按识别结果还原原文后切句：两段都一次切开，16 个切点全部落在正确的两句之间，且都在 0.3–0.7 s 的静音中间（豆包句间停顿约 0.55–0.7 s，换条处不需要补停顿）。实测语速 4.46 / 6.22 → 默认语速由 5.0 改为 4.5。静音段 RMS 最大 56，正文约 5000–6300，阈值 300 余量充足。
- **切回单句**：沿用旧版 `split_batch`（动态规划 + 语速检查）。切不开、接近 120 秒（可能截断）、有声部分不到估算一半（可能漏读）→ 该段对半拆开（尽量在新闻之间）重新请求，直到单句。
- **时间轴衔接**：切出的每句保留它那一半自然停顿（`lead` / `tail`）；同一段同一条新闻内原样拼回不插停顿；换条、钩子后、两段之间只把停顿补足到 0.40 / 0.30 / 0.14 秒，不缩短。场景、卡片高亮、字幕按真实说话时刻。
- **实测语速记忆**：每段切好后把实测语速写进 `outputs/_state/doubao_speech_rate.json`，下次分段取近 10 段中位数。
- **端到端验证**（本环境）：11-04 实时数据 + 兜底稿 → 2 段请求；每段用 edge 整段一次合成并在开头按豆包结构加真实朗读的“内容由豆包AI生成”，模拟豆包返回 → 两段水印都在 2.58 秒处切掉，两段都一次切开（每句语速比 0.88–1.14），36 句逐句语音识别核对开头全部正确 → 混音、渲染、自检 9 项全过（196.9 秒），封面与发布包正常。
- 顺带修了兜底稿的三个既有问题（实时数据下兜底稿会校验失败，破坏“每天一定出片”）：加句号后超过 42 字；`1,024` 这类数字在逗号处被切成 `024`；素材里的链接进了旁白（`https。//。`），以及在括号中间截断。
- 已删除：`--tts-mode`、声音克隆、`DOUBAO_TEMPO` / 整片变速 `AI_DAILY_OUTPUT_TEMPO`、3 分钟上限（`RUNDOWN_MAX_SECONDS`、`BUDGET_TOTAL_NARRATION_MAX`、自检的 180 秒上限）。渲染工作区沿用同事改的 `hf-workspace/`。

## 修改过的既有 bug

- 改写某句旁白后重跑，配音会沿用旧音频（`voice._drop_stale_audio`）
- 小红书标题因中英文空格被截断（`publish._compact`）
- 兜底稿在实时数据下会校验失败（加句号后超过 42 字；`1,024` 被切成 `024`；链接和括号截断进了旁白）（`director._fit` / `_clauses` / `_sentences`）
- 同事提交带来的：封面阶段 NameError（`covers.py`）；去水印误切所有音频；`test_daily.py` 被替换

## 关键文件

| 文件 | 作用 |
|---|---|
| `ai_morning_brief/daily.py` | 编排器（阶段、交接、兜底、CLI） |
| `ai_morning_brief/newsroom.py` + `aihot_v2.py` | 取数、补充素材、合并、选题（权重 `config.py` 的 `HEAT_*`） |
| `ai_morning_brief/material.py` | 素材包、相关性判断、字数预算、时长估算 |
| `ai_morning_brief/director.py` | 导演稿任务、校验器、兜底稿、逐句单元（契约 6.2） |
| `ai_morning_brief/voice.py` / `timeline.py` / `audio_mix.py` | 豆包长录音分段/去水印/切句、edge 逐句配音、时间轴、混音 |
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
python3 -m unittest discover -s ai_morning_brief/tests          # 应为 152 OK
python3 -m ai_morning_brief.daily --date 2026-11-04 --tts edge  # 停在 director 交接（用当天日期即可）
cp docs/handoff/2026-11-04-director_plan.json outputs/2026-11-04/artifacts/director_plan.json  # 只在同一天、pool 未变时可直接用
python3 -m ai_morning_brief.daily --date 2026-11-04 --check-plan
python3 -m ai_morning_brief.daily --date 2026-11-04 --tts edge  # 跑完全部阶段
```

注意：热点与站内搜索只有当前快照，换一天复现时选题会不同，参考导演稿里的 item_id 可能不在 pool 里而校验失败——那时按任务文件重新写稿即可。网络需允许 `aihot.news`、`speech.platform.bing.com`（edge-tts）、`raw.githubusercontent.com`（字体）。`outputs/` 不在仓库里。

## 历史

- 第一期 10-30 导演稿：`docs/handoff/2026-10-30-director_plan.json`（6.0 契约，已不能通过当前校验，仅作对比）
