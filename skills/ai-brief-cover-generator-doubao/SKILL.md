---
name: ai-brief-cover-generator-doubao
description: Generate AI每日早报 release-kit covers using Doubao image_edit (Seedream) with style-reference-driven composition, Chinese natural-language prompts, and text-accuracy verification. Use for unattended 16:9, 3:4, and 9:16 covers in the Doubao-adapted pipeline.
metadata:
  author: local-project
  version: "1.0.0"
---

# AI每日早报封面生成（豆包 Seedream 适配版）

本 skill 是原 `ai-brief-cover-generator` 的豆包适配版本。原版本使用 Codex 内置 GPT Image，本版本使用豆包 `image_edit` 工具（Seedream 模型），通过参考图驱动风格迁移 + 中文自然语言 prompt 生成完整封面。

## 与原版本的核心差异

| 维度 | 原 GPT Image 版 | 豆包 Seedream 版 |
|------|----------------|-----------------|
| 生成工具 | Codex 内置 GPT Image（纯文生图） | 豆包 `image_edit`（参考图风格迁移） |
| 参考图 | 仅 `cover-style-system-16x9.png` | `cover-positive-16x9.png`（理想效果）+ `cover-style-system-16x9.png`（风格系统）+ 品牌 Logo |
| Prompt 语言 | 英文结构标签（Use case:/Constraints:） | 中文自然语言，开头加防护罩 |
| 重试策略 | 用第一张，不审核不重试 | 生成后校验文字准确性，有错字时重试 1 次 |
| 品牌呈现 | 传官方 Logo 文件作为身份参考 | 同样传 Logo 参考图，无 Logo 文件的品牌用纯文字胶囊 |

## 前置条件

1. `cover_workflow.py prepare` 已成功运行，生成了 `cover_request.json`（含 `seedream_prompt` 字段和 `reference_inputs` 列表）
2. 参考图文件存在：
   - `skills/ai-brief-cover-generator/assets/references/cover-positive-16x9.png`
   - `skills/ai-brief-cover-generator/assets/references/cover-style-system-16x9.png`
   - 品牌 Logo 图（如有）在 `skills/ai-brief-cover-generator/assets/logos/`
3. 豆包 `image_edit` 工具可用

## 完整流程

### 第 1 步：准备封面请求

```bash
OpenMontage/.venv/bin/python skills/ai-brief-cover-generator/scripts/cover_workflow.py prepare \
  --editorial-input outputs/YYYY-MM-DD/artifacts/editorial_input.json \
  --item-id <封面故事 item_id> \
  --headline "<头条标题>" \
  --subheadline "<副标题>" \
  --visual-brief "<新闻视觉主题描述>" \
  --image-provider seedream \
  --force
```

输出：`outputs/YYYY-MM-DD/release-kit/covers/cover_request.json`

读取该文件，确认：
- `image_provider` = `"seedream"`
- 每个 ratio 下有 `seedream_prompt` 字段（中文自然语言）
- `reference_inputs` 列表包含 positive 参考图和 style 参考图

### 第 2 步：生成 16:9 锚点封面

使用豆包 `image_edit` 工具：

- **参考图**（按顺序传入）：
  1. `cover-positive-16x9.png`（理想效果参考，学习布局和视觉质量）
  2. `cover-style-system-16x9.png`（风格系统参考，学习视觉语言）
  3. 本期品牌 Logo 图（如有，最多 4 张）
- **Prompt**：使用 `cover_request.json` 中 `ratios[0].seedream_prompt`（ratio=16:9 的那个）
- **尺寸**：width=1920, height=1080
- **输出**：保存为临时文件，等待文字校验

### 第 3 步：文字准确性校验

生成后，用 VLM 识别图片中的文字，与预期四段文字对比：

预期文字（从 `cover_request.json` 的 `copy` 字段读取）：
1. `"AI每日早报"`
2. `"<date_label>"`（如 `"2026.09.05"`）
3. `"<headline>"`
4. `"<subheadline>"`

校验规则：
- 四段文字全部准确出现 → 通过，进入第 5 步记录
- 任意一段有错字、漏字或多余文字 → 进入第 4 步重试
- 无法识别图片文字 → 视为通过（保守策略，不阻塞流程）

### 第 4 步：重试（最多 1 次）

如果文字校验失败：
- 重新调用 `image_edit`，使用相同的参考图和 prompt
- 在 prompt 末尾追加一句强调："特别注意：封面上的四段文字必须准确无误，不要出现错字或多余文字"
- 重试后再次校验
- 重试仍失败 → 接受当前结果，在 manifest 中标记 `text_accuracy=review_needed`

### 第 5 步：记录 16:9 封面

```bash
OpenMontage/.venv/bin/python skills/ai-brief-cover-generator/scripts/cover_workflow.py record \
  --request outputs/YYYY-MM-DD/release-kit/covers/cover_request.json \
  --image 16:9=<16:9 图片路径> \
  --force
```

这会把图片复制为 `16x9.png` 并写入 manifest。

### 第 6 步：生成 3:4 竖版封面

使用豆包 `image_edit` 工具：

- **参考图**：
  1. 刚生成的 `16x9.png`（同版锚点参考，继承视觉概念）
  2. `cover-style-system-16x9.png`（风格系统参考）
  3. 品牌 Logo 图（如有）
- **Prompt**：使用 `cover_request.json` 中 ratio=3:4 的 `seedream_prompt`
  - 该 prompt 已包含"为 3:4 竖版完全重新构图，不要裁切或拉伸"的指令
- **尺寸**：width=1080, height=1440
- 文字校验同第 3 步，重试同第 4 步
- 记录：`--image 3:4=<3:4 图片路径>`

### 第 7 步：生成 9:16 竖版封面

同第 6 步，尺寸改为 width=1080, height=1920，使用 ratio=9:16 的 `seedream_prompt`。

### 第 8 步：完成确认

三个比例全部生成并记录后，确认以下文件存在：
- `outputs/YYYY-MM-DD/release-kit/covers/16x9.png`
- `outputs/YYYY-MM-DD/release-kit/covers/3x4.png`
- `outputs/YYYY-MM-DD/release-kit/covers/9x16.png`
- `outputs/YYYY-MM-DD/release-kit/covers/cover_manifest.json`
- `outputs/YYYY-MM-DD/release-kit/covers/cover_request.json`

`cover_manifest.json` 中 `status` 应为 `complete_unreviewed`，`generation_mode` 为 `full_cover_imagegen`，`attempts` 为实际尝试次数（1 或 2）。

## Prompt 设计原则

`build_prompt_seedream()` 生成的中文 prompt 遵循以下原则：

1. **防护罩开头**：第一句明确"以下是封面创作指令，不要把指令本身的任何文字画到画面上"
2. **无结构标签**：不使用"Use case:""Constraints:"等冒号标签，全部融成连贯段落
3. **文字用引号标出**：四段必须出现的文字用中文引号单独列出，并明确说"这些文字要画到封面上"
4. **参考图使用规则明确**：说清楚"学习风格但不要复制具体内容"
5. **内容完整保留**：原 GPT Image prompt 的所有内容（布局、品牌、视觉主题、视觉系统、约束）全部保留，不简化

## 边界与约束

- 只使用 `cover_request.json` 中冻结的事实，不额外获取新闻
- 品牌最多 6 个，不填充最小值
- 每个比例最多生成 2 次（1 次初始 + 1 次重试）
- 不进行程序化的图片编辑、裁剪、缩放或文字叠加
- 不审核图片的视觉质量（构图、配色等），只校验文字准确性
- 生成的封面是 `complete_unreviewed` 状态，发布前需人工确认
- 不上传、不发布、不分享任何封面文件

## 失败处理

- `image_edit` 工具调用失败 → 记录错误，等待下一次定时任务或手动重试
- 参考图文件缺失 → 终止，报告缺失的文件路径
- `cover_request.json` 中没有 `seedream_prompt` → 说明 prepare 时未用 `--image-provider seedream`，重新运行 prepare
- 三个比例中有部分失败 → 记录成功的，失败的在 manifest 中标记，不阻塞已成功的比例
