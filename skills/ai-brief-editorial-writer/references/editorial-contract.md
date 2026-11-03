# Director plan v6.0 contract

```json
{
  "version": "6.0",
  "picks": {"headlines": ["<item_id>"], "flashes": ["<item_id>", "<item_id>"]},
  "hook": {"text": "钩子，可含一对【】", "keyfact": "一句客观事实"},
  "stories": [
    {"item_id": "<headline id>", "role": "headline", "subject": "主体", "title": "标题",
     "cards": [{"label": "≤6字", "headline": "≤16字", "body": "≤48字，可含一对【】"}],
     "narration": [{"text": "一句话。", "card": 1}]},
    {"item_id": "<flash id>", "role": "flash", "subject": "主体", "title": "标题",
     "point": "≤56字", "narration": [{"text": "一句话。"}]}
  ]
}
```

| 字段 | 规则 |
|---|---|
| `picks.headlines` | 1–2 条，来自 `pool` |
| `picks.flashes` | 0–3 条，来自 `pool`；头条 + 快讯 ≤ 4，不重复 |
| `hook.text` | ≤28 字；最多一对【】；数字/英文须在头条素材中 |
| `hook.keyfact` | ≤60 字；不用【】 |
| `stories` | 先头条（role=headline）后快讯（role=flash），与 picks 顺序一致 |
| `subject` | ≤20 字；用于主体胶囊，若匹配品牌库会自动显示官方 Logo |
| `title` | ≤32 字 |
| 头条 `cards` | 2–5 张；label ≤6、headline ≤16、body ≤48（可含一对【】） |
| 头条 `narration` | 每句 ≤42 字、句末“。！？”；`card` 为 1 起的整数，非递减，覆盖每张卡；合计 ≤170 字 |
| 快讯 `point` | ≤56 字 |
| 快讯 `narration` | 1–2 句，合计 ≤70 字 |
| 全片 | 钩子 + 全部旁白 ≤360 字（约 75 秒） |

事实规则：数字（`\d+(\.\d+)?`）和英文词必须出现在该新闻的 AIHOT 素材（title、hot_title、summary、digest、related_titles）中；不添加来源没有的因果、预测或建议；保留“尚未验证”等限定。

读法：`display_text` 进字幕和画面，`spoken_text` 由 `writing.normalize_with_ledger` 自动生成，不要手写读法。

兜底：校验失败 2 次或 `--no-agent` 时，程序用 `director.fallback_plan` 从 AIHOT 原文生成一份合规稿件（允许摘抄原文）。
