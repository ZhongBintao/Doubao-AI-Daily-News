# Director plan v6.1 contract

```json
{
  "version": "6.1",
  "picks": {"headlines": ["<item_id>"], "flashes": ["<item_id>", "<item_id>"]},
  "hook": {"text": "钩子，可含一对【】", "keyfact": "一句客观事实"},
  "stories": [
    {"item_id": "<headline id>", "role": "headline", "subject": "主体", "title": "标题",
     "cards": [{"label": "发生了什么", "headline": "≤16字结论", "body": "≤60字细节，可含一对【】"}],
     "narration": [{"text": "一句话。", "card": 1}]},
    {"item_id": "<flash id>", "role": "flash", "subject": "主体", "title": "标题",
     "point": "≤60字，发生了什么", "why": "≤60字，所以呢",
     "narration": [{"text": "讲 point。", "card": 1}, {"text": "讲 why。", "card": 2}]}
  ]
}
```

| 字段 | 规则 |
|---|---|
| `picks.headlines` | 1–3 条，来自 `pool`；多于 1 条时只能选 `budget.can_headline=true` 的 |
| `picks.flashes` | 0–5 条，来自 `pool`；不与头条重复 |
| `hook.text` | ≤24 字；最多一对【】；数字/英文须在第一条头条的素材中 |
| `hook.keyfact` | ≤60 字；不用【】 |
| `stories` | 先头条（role=headline）后快讯（role=flash），与 picks 顺序一致 |
| `subject` | ≤20 字；用于主体胶囊，若匹配品牌库会自动显示官方 Logo |
| `title` | ≤32 字 |
| 头条 `cards` | 张数按 `budget.cards`；`label` 取自 `发生了什么 / 关键数字 / 怎么做到的 / 各方说法 / 背景 / 为什么重要 / 对你的影响 / 还没确定 / 下一步`，不重复；`budget.needs_depth_card=true` 时至少一张 `各方说法 / 背景 / 为什么重要 / 对你的影响`；headline ≤16、body ≤60（可含一对【】），两者不能重复 |
| 头条 `narration` | 每句 ≤42 字、句末“。！？”；`card` 为 1 起的整数，非递减，覆盖每张卡；合计落在 `budget.headline_narration` |
| 快讯 `point` / `why` | 各 ≤60 字，可含一对【】；`budget.flash_sentences` 下限 ≥2 时 `why` 必填，且不能与 point 重复 |
| 快讯 `narration` | 句数落在 `budget.flash_sentences`；`card` 可选，1 = point、2 = why；合计落在 `budget.flash_narration` |
| 全片 | 钩子 + 全部旁白 ≤900 字（约 3 分钟） |

`budget` 随素材多少浮动（`config.STORY_BUDGETS`）：

| 素材深度 | 头条旁白 | 头条卡片 | 快讯旁白 | 快讯句数 |
|---|---|---|---|---|
| rich（≥700 字或多源综述） | 150–300 字 | 3–6 | 45–110 字 | 2–3 |
| medium（≥300 字） | 100–220 字 | 3–5 | 40–100 字 | 2–3 |
| thin | 60–140 字（只在安静日做头条） | 2–4 | 25–80 字 | 1–3 |

下限不会超过素材字数的 40%，卡片下限不会超过素材的句子数，素材少时不强求。

事实规则：数字（`\d+(\.\d+)?`）和英文词必须出现在该新闻 AIHOT 素材包的 `facts` 或 `background` 中（含来源名）；`angle_hint` 不算证据。不添加来源没有的因果、预测或建议；保留“尚未验证”等限定。素材只来自 AIHOT 网站本身，从不打开原文链接。

读法：`display_text` 进字幕和画面，`spoken_text` 由 `writing.normalize_with_ledger` 自动生成，不要手写读法。

兜底：校验失败 2 次或 `--no-agent` 时，程序用 `director.fallback_plan` 从 AIHOT 素材包生成一份合规稿件（允许摘抄原文，按句子类型分配卡片标签）。
