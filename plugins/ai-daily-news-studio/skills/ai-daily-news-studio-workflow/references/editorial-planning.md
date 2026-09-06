# AI每日早报 editorial plan v5.1

## Source selection metadata

The runtime freezes four independent AIHOT requests with
`mode=selected&window=24h&by=timeline` and categories `ai-models`,
`ai-products`, `industry`, and `paper`. `window=24h` is rolling from request
time. The candidate report preserves each item's raw `score`, both links,
dimension-local `rank`, `rank_percentile`, `tier`, and decision reason. A score
is never a cross-dimension threshold and missing scores remain eligible.
Video cards and narration must not display these metadata fields.

`editorial_input.json`（含冻结 source details）是唯一事实源。每条入选资讯恰好
出现一次，所有显示文案和播音文案都可追溯到 exact claims。

## 概览

每个 story 必须写 `overview_text` 和 `overview_claim_ids`。文案包含主体与
具体动作、结果、能力、限制或冲突，并至少引用一条 summary/detail claim；不得用
`navigation_title`、品牌名或“事件解读”等短标签代替。渲染器按约 128 个可见
单位分页，每页固定停留 5 秒；不得按字数、单位数或音频长度延长概览页。

## 叙事与字幕

每个 beat 有稳定 `beat_id`、完整事实语境和 claim IDs。beat 可包含多句和完整
解释，不设 28 字、总字数或视频时长上限，也不强制 impact/action。影响、行动、
限制只在来源支持时写。finalizer 再按标点和画面宽度拆成不超过 28 个可见单位的
连续字幕，保留 beat/claim 上下文。

## 卡片与导航

卡片数量由有效 claims 决定，不设上限和固定正文长度。每张卡有稳定 ID、subject
和独立信息职责；渲染器每页排 3–5 张并完整翻页，不截断。唯一导航栏位于顶部，
仅含按 `presentation_order` 排列的“主体＋具体变化”标题；禁止“事件解读”“引争议”
“最新动态”、省略号和无主体标签。

## 原文视觉

有 schema-5 素材时，writer 可给后置 beat 写 `visual_asset_id`。X 新闻最多一个
素材，普通网页可依次绑定首屏素材和至多一张正文图片；每个 beat 的 claim IDs
必须与对应素材 evidence claims 相交，且之前至少有一个非视觉 beat 先展示卡片。
图片和视频都使用同一直接叠加层。

## Required shape

```json
{
  "version": "5.0",
  "prompt_version": "codex-editorial-v5.1",
  "writer": {"skill": "ai-brief-editorial-writer", "version": "4.1", "status": "approved"},
  "stories": [{
    "story_kind": "single",
    "subject": "明确主体",
    "navigation_title": "主体发布具体能力",
    "overview_text": "主体发布了什么，以及来源披露的具体结果",
    "overview_claim_ids": ["claim-summary"],
    "presentation_order": 1,
    "narration": {"beats": [
      {"beat_id": "story-01-beat-01", "type": "hook", "text": "...。", "claim_ids": ["claim-title"]},
      {"beat_id": "story-01-beat-02", "type": "evidence", "text": "完整解释...。", "claim_ids": ["claim-detail"], "visual_asset_id": "source-item-visual-01"}
    ]},
    "cards": []
  }]
}
```

For a short same-dimension `brief_group`, set `story_kind` to
`brief_group`, provide 2–4 `source_item_ids`, `group_label`, and an
`overview_items` entry for every item. Provide exactly one card per source
item with `card.source_item_id`, and map every card from a corresponding beat
through `beat.card_ids`. Use `cards_per_page=4`; do not attach source visuals.
Keep dimension leaders and complex multi-claim stories as `single`. Groups of
5–8 short items must be balanced as 3+2, 3+3, 4+3, or 4+4, with no singleton.

Historical v2–v4 plans remain replayable. New production plans must use v5.
