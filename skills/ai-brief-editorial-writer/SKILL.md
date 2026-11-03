---
name: ai-brief-editorial-writer
description: Write the AI每日早报 v6 director plan (director_plan.json) — pick stories from the AIHOT pool and fill the hook, labelled cards and per-sentence narration from each event's AIHOT material pack, under budgets that scale with the material, following the bundled examples.
metadata:
  author: local-project
  version: "6.1.0"
---

# AI每日早报 v6 · 导演稿写作

你只需要做“选择题 + 填空题”，版式、动画、时长都由程序负责。目标是**把每条新闻讲清楚、讲透**：不只说发生了什么，还要讲关键数字、各方说法、背景和为什么重要。

## 输入与输出

- 读：`outputs/YYYY-MM-DD/artifacts/director_task.json`（steps、rules、budgets、pool、recommended、examples、output_template）
- 写：`outputs/YYYY-MM-DD/artifacts/director_plan.json`（`"version": "6.1"`）
- 校验：`python -m ai_morning_brief.daily --date YYYY-MM-DD --check-plan`

## 素材在哪里

`pool` 里每条新闻都带一个 AIHOT 素材包，**只来自 AIHOT 网站本身**：

| 字段 | 内容 | 能否引用 |
|---|---|---|
| `facts` | AIHOT 摘要、AIHOT 综述、最新进展、各来源报道摘要、同事件其他报道 | 可以，事实依据 |
| `background` | AIHOT 里的相关事件、此前报道 | 可以，但要说明是“此前”或“相关”的事 |
| `angle_hint` | AIHOT 编辑写的“为什么值得看” | **不能引用**，只用来决定讲哪个角度 |
| `budget` | 这条新闻的字数范围、卡片数、能否做头条 | 按它写 |

## 七个步骤

1. **选新闻**：头条最多 3 条（只能选 `budget.can_headline=true` 的），快讯最多 5 条。条数由当天内容决定，不必凑满；拿不准就**照抄 `recommended`**（程序已按 3 分钟上限排好）。
2. **读素材**：先把 `facts` 和 `background` 读完，再动笔。
3. **写钩子**：`hook.text`（≤24 字）说出第一条头条最抓人的点，可用一对【】；`hook.keyfact`（≤60 字）用一句客观事实说清“谁做了什么”。钩子写法见 examples 01–04。
4. **写头条卡片**：卡片数按 `budget.cards`。每张卡的 `label` 从下面 9 个里**选一个**，同一条新闻不重复：

   `发生了什么` · `关键数字` · `怎么做到的` · `各方说法` · `背景` · `为什么重要` · `对你的影响` · `还没确定` · `下一步`

   - 第一张通常是“发生了什么”。
   - `budget.needs_depth_card=true` 时，至少要有一张 `各方说法 / 背景 / 为什么重要 / 对你的影响`。
   - `headline`（≤16 字）写结论，`body`（≤60 字）补细节，两者不能是同一句话换个说法；`body` 可用一对【】高亮数字。
5. **写头条旁白**：逐句写（每句 ≤42 字，句末“。！？”），`card` 标明讲第几张卡（从 1 开始、按顺序、每张卡至少一句）。旁白要**解释**卡片，不要照念；合计字数落在 `budget.headline_narration` 范围内。
6. **写快讯**：`point`（≤60 字）讲发生了什么；`why`（≤60 字）讲“所以呢”——反差、影响或各方反应（素材足够时必填）。旁白写 `budget.flash_sentences` 句：第 1 句讲 point（`card: 1`），后面讲 why（`card: 2`），合计落在 `budget.flash_narration` 范围内。
7. **校验**：运行校验命令，按报错逐条改，最多 2 次。还不行也没关系，程序会自动用兜底稿出片。

## 必须遵守

- 所有**数字、英文名、产品名**必须原样出现在该条新闻的 `facts` 或 `background` 里。数字写法要一致（素材写 9 就写 9，写“九个”就写“九个”）。
- “为什么重要 / 对你的影响”只复述素材里**已经写出的**意义或影响，不自己推测、预测或给建议。
- 素材说“尚未验证 / 尚无复现 / 尚未确定 / 说法不一”时，用“还没确定”卡片或一句旁白保留这层意思。
- “各方说法”写清是谁说的：“据 TechCrunch，……”“马斯克称……”。来源之间说法不一时，分别写出来。
- 不写分数、链接、“AIHOT”。
- 字数按可见字符算，英文单词按字母数算（`Fireworks` = 9 字），英文多时写短一点。全片旁白 ≤900 字（约 3 分钟）。

## 常见报错怎么改

| 报错 | 改法 |
|---|---|
| `xxx 不在 pool 里` | picks 里只能填 pool 中出现的 `item_id` |
| `素材太少，讲不深，请改为快讯` | 把它从 picks.headlines 移到 picks.flashes |
| `超过 N 字` | 删掉修饰语或换更短的说法 |
| `只有 N 字，这条素材足够讲到 …` | 从 facts 的各来源报道、background 里补充细节、背景或各方说法 |
| `这些数字或英文在该新闻素材里找不到` | 改成素材里的原样写法，或删掉 |
| `label：只能从这些标签里选一个` | 换成 9 个标签之一 |
| `至少要有一张“各方说法 / 背景 / 为什么重要 / 对你的影响”` | 加一张这样的卡，内容从各来源报道或 background 里找 |
| `小标题和正文几乎是同一句话` | 小标题写结论，正文写具体细节（数字、谁、怎么做） |
| `“各方说法”要写清是谁说的` | 正文加上“据 X，”或“X 称” |
| `素材里提到“尚未验证 / 尚未确定”` | 加一张“还没确定”卡，或加一句旁白 |
| `必须以句号、问号或感叹号结尾` | 句末加“。” |
| `card 编号必须从小到大` / `第 N 张卡没有被讲到` | 按卡片顺序给每句标 card，每张卡至少一句 |
| `顺序和 picks 不一致` | stories 先按 picks.headlines 写头条，再按 picks.flashes 写快讯 |

完整字段契约见 [references/editorial-contract.md](references/editorial-contract.md)，示范见 `references/examples/*.json`（01–04 头条，05 快讯）。
