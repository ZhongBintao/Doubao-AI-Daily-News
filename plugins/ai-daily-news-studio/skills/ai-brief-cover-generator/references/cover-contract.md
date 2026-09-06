# GPT Image native full-cover contract

Read this reference before preparing or generating release-kit covers.

## Ratios

The unattended default is all three standard ratios:

| Ratio | Requested size | Suggested domestic uses |
| --- | --- | --- |
| `16:9` | `1920×1080` | Bilibili landscape cover, landscape video, web/news card |
| `3:4` | `1080×1440` | Xiaohongshu first image, WeChat article poster, portrait news card |
| `9:16` | `1080×1920` | Douyin, Kuaishou, and WeChat Channels vertical cover |

Use a user-specified set when provided. A custom ratio requires an exact target
size. Requested sizes are prompt metadata, not post-generation normalization
targets; no code resizes or rejects a result for its actual dimensions.

## Frozen facts and visual brief

- Read one frozen `editorial_input.json`; never fetch or supplement daily facts.
- Keep the cover story equal to `release_plan.cover_story_item_id` when a release
  plan exists. Otherwise rank selected items by impact, recognized entities,
  visual anchors, AIHOT score, and frozen order.
- Freeze exactly four visible text fields: `AI每日早报`, `YYYY.MM.DD`, one
  headline, and one subheadline. Every entity, number, unit, event, and factual
  relationship must be supported by the selected title or summary.
- Freeze one specific `news_visual_brief` derived only from the headliner. It
  should describe the event's visual metaphor, actors, objects, and direction
  without adding analysis, forecasts, or unsourced metrics.

## Edition brands

Extract brands from every selected story. Add all headliner brands first, then
walk the final editorial story array (the video presentation order), deduplicate,
and stop at six. Never fill a minimum: an edition with one, two, or three actual
brands keeps only those brands.

Bundled or run-local official Logo files are GPT Image identity references.
They are never composited by code. A new run-local manifest records `name`,
`aliases`, `source_page_url`, `asset`, and `sha256`. If a source-present brand
has no verified file, use its exact name as identity text; never create a
lookalike mark. Additional brand names absent from all selected title/summary
text are rejected before generation.

## Active style reference

`assets/references/cover-style-system-16x9.png` is the active visual-system
reference. Tell GPT Image to learn only its editorial infographic language,
hierarchy, warm ivory/black/orange/deep-teal palette, typography character,
brand capsules, illustration texture, shadows, and information density.
Explicitly ignore and never copy the example's date, copy, numbers, companies,
or news facts.

`assets/references/cover-positive-16x9.png` is historical audit material only.
Do not use it as an image-generation input.

## Complete-cover prompt

Every ratio prompt must include these sections and dynamic values:

```text
Use case: ads-marketing
Asset type: AI每日早报完整平面封面，{ratio}

Primary request:
一次性生成可直接发布的最终封面。文字排版、品牌标识、新闻插画、
信息图元素、层级、间距和整体构图必须统一生成；不会进行任何后期
排版、Logo叠加、裁切或修正。

Input images:
- Image 1：主动视觉系统参考；只学习视觉语言，忽略全部旧内容。
- 本期官方品牌身份参考；只用于忠实呈现品牌。
- 竖版额外使用本期16:9成品；继承概念与家族气质并重新构图。

Text — verbatim, each exactly once:
"AI每日早报"
"{YYYY.MM.DD}"
"{headline}"
"{subheadline}"

Current-edition brands:
{actual edition brands, maximum six}

News visual brief:
{source-grounded headliner visual brief}
```

The prompt also requires a bright warm-ivory background, giant black headline,
orange-red emphasis and rounded information bar, deep-teal/orange editorial
infographic illustration, light dimensional material, soft shadows, and a
single coherent story joining metaphor, trend/data paths, device/model symbols,
and brand treatment. It forbids extra copy, facts, pseudo-text, invented or
merged brands, player/platform UI, phone frames, QR codes, watermarks, and
signatures.

## One-shot generation order

Generate the anchor first (`16:9` when selected, otherwise the first ratio).
Generate each remaining ratio once using the active style image, current anchor,
and official identity references. Portraits must be newly composed; no crop,
stretch, padding, Pillow overlay, or local layout code is allowed.

Use the first file returned for every ratio. There is no visual approval,
candidate batch, targeted edit, retry, family check, or rejected-attempt flow.
A failed tool call or absent output is a machine failure, not permission for a
second model call.

## Schema-5 outputs

Write only under `outputs/YYYY-MM-DD/release-kit/covers/`:

- `cover_request.json`: complete prompts, active style reference, official
  identity references, frozen copy/brief, brand order, and generation order.
- `16x9.png`, `3x4.png`, `9x16.png`: first GPT Image files copied byte-for-byte
  for the selected ratios.
- `cover_manifest.json`: `status=complete_unreviewed`,
  `generation_mode=full_cover_imagegen`, `post_processing=false`,
  `attempts=1`, and each result's `generated_file`, byte size, and SHA-256.

The manifest deliberately has no `normalized_file`, `brand_render`,
`visual_review`, approval status, rejected attempts, or family review.
