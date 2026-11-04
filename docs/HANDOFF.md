# 交接说明 · v6 竖屏版（2026-11-03，第二轮）

新对话请先读本文件，再读 `AGENTS.md` 与 `docs/design/v6-vertical-template.md`（第 10 节是本轮修改记录）。

## 当前状态

- 分支：`design/v6-vertical-template`（**不要合并到 main，不要开 PR**，用户明确要求单独放分支）
- 10 阶段编排器 `python -m ai_morning_brief.daily`，123 个离线测试全部通过
- 第一期（10-30，44 秒）暴露“内容单薄 + 画面缺陷”，本轮已全部修改（见下）
- 第二期实跑：`--date 2026-11-03 --tts edge`，导演稿由 Agent 按 v6.1 契约手写（`docs/handoff/2026-11-03-director_plan.json`），成片 155 秒，3 条头条 + 3 条快讯，自检全部通过，等待用户看片反馈

## 用户已确定的决定

1. 发布平台：TikTok + 小红书，竖屏；全自动运行，不需要人工确认；定时时间由用户自己设置
2. 视觉：基于原 v5 模板演进（米白纸张底、陶土橙、深青、白色圆角卡片），不要另起风格；必须对不同类型新闻通用
3. 必须照顾能力较弱的模型：AI 只做“选择题 + 填空题”，有模板、示范、校验器、兜底
4. **内容只来自 AIHOT 网站本身**：可以用 AIHOT 的综述、各来源报道摘要、站内搜索、相关事件；**不抓取、不打开 AIHOT 条目里的外部原文链接**（合规考虑）
5. **内容质量优先**，不为短视频节奏牺牲完整性；**时长上限 3 分钟**
6. **条数灵活**：头条最多 3 条、快讯最多 5 条，按当天内容决定，不固定
7. 用户已取得 AIHOT 书面授权，无需再提醒

## 本轮改了什么

内容（解决“重磅也不充实、快讯更单薄”）：

- `material.py` 素材包：AIHOT 综述 + 最新进展 + 各来源报道 + 站内搜索（`/items?mode=all&q=`）到的同事件报道 + 相关事件/此前报道；AIHOT 推荐理由只作角度提示。每个事件分 rich / medium / thin
- `newsroom.enrich()`：第二轮只读 AIHOT 的取数（前 12 个事件搜索、前 4 个热点事件取相关事件），全部冻结进快照
- 同一故事线合并（例：Anthropic 招股书、营收、算力承诺三条合成一条）
- 节目单：重磅且素材非 thin → 头条（≤3）；其余 ≥76 分 → 快讯（≤5）；按 5.5 单位/秒估算，超 3 分钟先删最弱快讯
- 导演稿 6.1：卡片标签 9 选 1（必须有“各方说法/背景/为什么重要/对你的影响”之一）；快讯 point + why、2–3 句；字数上下限随素材浮动；新增“小标题正文重复”“各方说法要写来源”“保留尚未验证”校验；5 个新示范；兜底稿也按素材包生成多卡片

画面：顶栏全片一份（修转场重影）；字幕按词换行、单框收缩、去句末标点、钩子页不显示字幕；换行规则补前后缀/助词/英文专名；图标按标签；卡片字号放大 + 手风琴式整页卡片 + 均衡分页；快讯两张卡；去掉高亮右移；Logo 胶囊配名字、白色 Logo 自动改深色；结尾“今天讲了”回顾；结尾语改为“关注我，每天看懂 AI 大事。”

修复的既有 bug：改写某句旁白后重跑，配音会沿用旧音频（`voice._drop_stale_audio`）。

## 暂未决定 / 待办

- **等用户看 11-03 成片反馈**
- **卡片/大标题文案风格**：用户希望头条、快讯的大标题和卡片小标题更抓眼球（制造悬念、好奇心），方案讨论中（见对话；尚未改代码）
- 已播记录存储位置：`outputs/_state/aired_events.json`（云电脑重置会丢，丢失时用前 3 天日报头条近似重建）
- 豆包逐句语音克隆未实测（本环境无豆包工具）；语速若明显慢于 edge（约 6.5 单位/秒），按 `SPEECH_CHARS_PER_SECOND` 调整
- `scripts/bootstrap_env.sh` 的改动未在豆包云电脑上验证
- 补跑 7 天以前的日期时，站内搜索与热点都拿不到，素材会退回薄素材（报告里可见 `sources_used`）
- 钩子页是否加通用视觉元素仍待定
- 我替用户做的默认选择（可推翻）：去掉概览页；结尾改为回顾 + 关注页（≥4.5 秒）；封面为 HTML 模板；头条卡片改为手风琴式整页显示

## 2026-11-04 已改（用户确认）

- 配色方案 A：按版块分色（头条陶土橙、快讯藏青），类别只作灰色文字标签；进度条每格保持所属版块颜色；封面同步
- 背景音乐：只用仓库内三段；opening 铺钩子并在第一条头条后淡出，middle-loop 在第一条头条出现时起播，ending 尾部对齐到最后一帧；说话时音乐低于人声约 18 dB；结尾页 ≥5.5 秒
- 结尾页去掉“资讯来源：AIHOT · 明天见”；来源改写进 TikTok / 小红书简介（`资讯来源：AIHOT（aihot.news）`），文案结尾语与视频统一

## 关键文件

| 文件 | 作用 |
|---|---|
| `ai_morning_brief/daily.py` | 编排器（阶段、交接、兜底、CLI） |
| `ai_morning_brief/newsroom.py` + `aihot_v2.py` | 取数、补充素材、合并、选题（权重 `config.py` 的 `HEAT_*`） |
| `ai_morning_brief/material.py` | 素材包、相关性判断、字数预算、时长估算 |
| `ai_morning_brief/director.py` | 导演稿任务、校验器、兜底稿、逐句单元（契约 6.1） |
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
python3 -m unittest discover -s ai_morning_brief/tests          # 应为 123 OK
python3 -m ai_morning_brief.daily --date 2026-11-03 --tts edge  # 停在 director 交接
cp docs/handoff/2026-11-03-director_plan.json outputs/2026-11-03/artifacts/director_plan.json
python3 -m ai_morning_brief.daily --date 2026-11-03 --check-plan
python3 -m ai_morning_brief.daily --date 2026-11-03 --tts edge  # 跑完全部阶段
```

注意：热点与站内搜索只有当前快照，换一天复现时选题会不同，参考导演稿里的 item_id 可能不在 pool 里而校验失败——那时按任务文件重新写稿即可。网络需允许 `aihot.news`、`speech.platform.bing.com`（edge-tts）、`raw.githubusercontent.com`（字体）。`outputs/` 不在仓库里。

## 历史

- 第一期 10-30 导演稿：`docs/handoff/2026-10-30-director_plan.json`（6.0 契约，已不能通过 6.1 校验，仅作对比）
