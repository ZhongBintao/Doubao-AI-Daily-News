---
name: ai-brief-editorial-writer
description: Write the AI每日早报 v6 director plan (director_plan.json) — pick stories from the AIHOT pool and fill the hook, cards and per-sentence narration under fixed budgets, following the bundled examples.
metadata:
  author: local-project
  version: "6.0.0"
---

# AI每日早报 v6 · 导演稿写作

你只需要做“选择题 + 填空题”，版式、动画、时长都由程序负责。

## 输入与输出

- 读：`outputs/YYYY-MM-DD/artifacts/director_task.json`（里面有 steps、rules、budgets、pool、recommended、examples、output_template）
- 写：`outputs/YYYY-MM-DD/artifacts/director_plan.json`
- 校验：`python -m ai_morning_brief.daily --date YYYY-MM-DD --check-plan`

## 六个步骤

1. **选新闻**：从 `pool` 选 1 条头条（两条都极重磅时可选 2 条）和 1–3 条快讯，总数 ≤ 4。拿不准就**直接照抄 `recommended`**。
2. **写钩子 `hook.text`**（≤28 字）：一句话说出头条最抓人的点。从 `examples` 的 4 种写法里挑一种：
   - 数字冲击：“只改 【0.016%】 的权重，数学成绩涨到 6 倍”
   - 直接结论：“Anthropic 正式启动 【IPO】，代码 ANT”
   - 与你有关：“ChatGPT 和 Codex 要【共用一份额度】了”
   - 规模反常：“一个月，【九个】 AI 开发平台接连出事”
   可以用一对【】标出一个重点（显示为强调色），最多一对。
3. **写 `hook.keyfact`**（≤60 字）：一句客观事实，“谁做了什么”。
4. **写每条新闻**（顺序：先头条，后快讯，与 picks 一致）：
   - 头条：`subject`（主体，如公司名）、`title`（≤32 字）、2–5 张 `cards`（`label` ≤6、`headline` ≤16、`body` ≤48）、逐句 `narration`。
   - 快讯：`subject`、`title`、`point`（≤56 字）、1–2 句 `narration`。
5. **旁白规则**：每句 ≤42 字，以“。！？”结尾；头条每句写 `card`（讲第几张卡，从 1 开始，按顺序，每张卡至少讲一句）。头条旁白合计 ≤170 字，快讯 ≤70 字，全片 ≤360 字。
6. **校验**：运行校验命令，按报错逐条改，最多 2 次。还不行也没关系，程序会自动用兜底稿出片。

## 必须遵守

- 所有**数字、英文名、产品名**必须原样出现在该条新闻的素材里（`title`/`hot_title`/`summary`/`digest`/`related_titles`）。数字写法也要一致（素材写 9 就写 9，写“九个”就写“九个”）。
- 不写来源没有的因果、预测、建议；素材说“尚未验证/尚无复现”时，卡片或旁白要保留这层意思。
- 不写分数、链接、“AIHOT”。
- 字数按可见字符算，英文单词按字母数算（`Fireworks` = 9 字），英文多时写短一点。

## 常见报错怎么改

| 报错 | 改法 |
|---|---|
| `xxx 不在 pool 里` | picks 里只能填 pool 中出现的 `item_id` |
| `超过 N 字` | 删掉修饰语或换更短的说法 |
| `这些数字或英文在该新闻素材里找不到` | 改成素材里的原样写法，或删掉 |
| `必须以句号、问号或感叹号结尾` | 句末加“。” |
| `card 编号必须从小到大` / `第 N 张卡没有被讲到` | 按卡片顺序给每句标 card，每张卡至少一句 |
| `顺序和 picks 不一致` | stories 先按 picks.headlines 写头条，再按 picks.flashes 写快讯 |

完整字段契约见 [references/editorial-contract.md](references/editorial-contract.md)，示范见 `references/examples/*.json`。
