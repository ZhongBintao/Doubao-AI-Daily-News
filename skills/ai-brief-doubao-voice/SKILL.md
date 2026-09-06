---
name: ai-brief-doubao-voice
description: Synthesize AI每日早报 narration audio using Doubao voice cloning (audio_to_audio_plus + example-audio.mp3 reference), proportional subtitle timing, and OpenMontage loudness mixing. v2.0 replaces the text-description TTS with reference-audio voice cloning.
metadata:
  author: local-project
  version: "2.0.0"
---

# AI早报语音合成（豆包语音克隆版）

本 skill 是原 `ai-brief-speech-quality` 的豆包适配版本 v2.0。v1.x 使用 `text_to_audio_plus` + 固定音色描述（"温柔桃子风格"）。v2.0 改用 `audio_to_audio_plus` 语音克隆，以项目根目录的 `example-audio.mp3` 为音色参考，字幕仍走比例估算路径。

## 与原版本的核心差异

| 维度 | 原 Azure 版 | v1.x 豆包描述式 | v2.0 豆包语音克隆（当前） |
|------|-----------|----------------|------------------------|
| TTS 工具 | Azure Speech SDK / REST API | `text_to_audio_plus` | `audio_to_audio_plus` |
| 音色 | Azure `zh-CN-Xiaochen:DragonHDLatestNeural` | 固定文字描述（"温柔桃子风格"） | 参考音频 `example-audio.mp3` 克隆 |
| 字幕对齐 | Azure WordBoundary 回调（字级精确） | 比例估算 | 比例估算 |
| API 密钥 | 需要 `AZURE_SPEECH_KEY` | 不需要 | 不需要 |
| 审核文件 | `azure_audio_manifest.json` | `doubao_audio_manifest.json` | `doubao_audio_manifest.json` (provider=doubao-voice-clone) |

## 语音克隆参考音频

- **文件**：项目根目录 `example-audio.mp3`
- **时长**：约 29.8 秒
- **要求**：单人说话、无背景音乐、无重叠语音、录音清晰
- **分发**：随 Git 仓库分发，云电脑重置后 `git pull` 即可恢复
- **替换**：如需更换音色，替换此文件即可（建议 10-30 秒干净人声），全片自动生效

## 固定合成指令（语音克隆）

所有 segment 的 TTS 生成必须使用以下格式，**逐字使用，不修改**：

> 工具：`audio_to_audio_plus`
> 参考音频：`@音频1` = 项目根目录 `example-audio.mp3` 的绝对路径
> Prompt：`"用参考音频的音色、语速和朗读风格，清晰朗读以下文字，不增删字词，无背景音无杂音：{spoken_text}"`

如需微调朗读风格，只修改本文件中的这段 prompt，所有 segment 自动生效。修改后需在 `doubao_audio_manifest.json` 的 `voice` 字段记录变更。

## 前置条件

1. `narration_plan.json` 已生成且通过校验
2. `editorial_quality_report.json` = `pass`
3. `example-audio.mp3` 存在于项目根目录
4. `doubao_tts_adapter.py` 可用（项目 `ai_morning_brief/` 目录下）
5. 豆包 `audio_to_audio_plus` 工具可用
6. ffmpeg 可用（用于音频归一化）

## 完整流程

### 第 1 步：生成 TTS 待合成清单

```bash
python3 -m ai_morning_brief.doubao_tts_adapter prepare --date YYYY-MM-DD
```

输出：
- `artifacts/doubao_tts_checklist.json`（待合成清单）
- 终端显示每个 segment 的 ID、字数、输出路径

检查清单中每个 segment 包含：
- `segment_id`（如 `intro`、`story-01`、`outro`）
- `spoken_text`（要合成的文字，已通过发音标准化）
- `char_count`（字数）
- `output_path`（音频保存路径）
- `minimum_duration_seconds`（最低时长要求，如 overview 段要求 5 秒）

### 第 2 步：逐段合成音频

对清单中的每个 segment，使用豆包 `audio_to_audio_plus` 工具（语音克隆）：

- **参考音频**：`@音频1` = 项目根目录 `example-audio.mp3` 的绝对路径
- **Prompt**：`"用参考音频的音色、语速和朗读风格，清晰朗读以下文字，不增删字词，无背景音无杂音：{spoken_text}"`
- **输出格式**：WAV
- **保存路径**：清单中 `output_path` 指定的路径（`assets/audio/narration-{segment_id}.wav`）

注意事项：
- 每段单独合成，不要把多段文字合并成一次调用
- 参考音频必须是同一个 `example-audio.mp3`，保持全片音色一致
- 合成后立即保存到指定路径，不要修改文件名
- 如果某段合成失败，记录失败的 segment_id，继续合成其他段，最后统一报告
- 不要对音频进行任何后期处理（降噪、变速、音量调整等），finalize 步骤会统一处理

### 第 3 步：检查合成进度

```bash
python3 -m ai_morning_brief.doubao_tts_adapter status --date YYYY-MM-DD
```

显示哪些 segment 已有音频、哪些还缺失。全部完成后进入第 4 步。

### 第 4 步：归一化与生成 manifest

```bash
python3 -m ai_morning_brief.doubao_tts_adapter finalize --date YYYY-MM-DD
```

finalize 自动完成：
1. 验证所有 segment 的音频文件存在且非空
2. 用 ffmpeg 统一转为 48kHz / 16bit / mono WAV
3. **自动填充短时长 segment**：如果某段音频时长低于 `minimum_duration_seconds`（如 overview 段要求 5 秒），用尾部静音填充到目标时长
4. 读取每段音频的实际时长
5. 生成 `artifacts/doubao_audio_manifest.json`，schema 与 Gemini manifest 兼容：
   - `provider`: `"doubao"`
   - `voice`: `"doubao-in-conversation-gentle-peach"`
   - `alignment_provider`: `"doubao-proportional"`
   - `alignment_quality`: `"approximate"`
   - `native_word_boundary`: `false`
   - 每段的 `spoken_duration_seconds`、`duration_seconds`、`display_text`

### 第 5 步：视频渲染（复用音频）

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline run \
  --date YYYY-MM-DD \
  --force \
  --reuse-source \
  --reuse-audio \
  --env-file .env \
  --speech-provider doubao-voice-clone
```

pipeline 自动完成：
1. 读取 `doubao_audio_manifest.json`
2. 因为 `provider=doubao` 且 `native_word_boundary=false`，自动走**比例估算字幕**路径
   - 字幕文字 100% 来自 `narration_plan.json` 的写稿文案（非识别结果）
   - 每段字幕的时间戳按该段音频时长占总时长的比例估算
   - 字幕单元以完整短句为单位，不在逗号处机械拆分
3. 背景音乐混音（OpenMontage 混音器）
4. HTML 模板物化
5. HyperFrames 渲染
6. 质量门禁

### 第 6 步：质量审核

渲染完成后，检查以下文件：

- `artifacts/doubao_audio_manifest.json` — 确认所有 segment 时长合理
- `artifacts/background-music.json` — 背景音乐配置
- `artifacts/mix_report.json` — 混音报告，确认：
  - 预闪避间隙在 ±0.5 LU 以内
  - sidechain attack/release = 30/350ms
  - 衰减不超过 4 dB
  - 最终响度接近 -16 LUFS
  - 真峰值 ≤ -1.5 dBTP
  - 无新增削波
- `artifacts/quality_report.json` — 整体质量门禁 = `pass`
- `artifacts/subtitles.srt` — 字幕文件，抽查文字准确性
- `renders/ai-daily-news-YYYY-MM-DD.mp4` — 最终视频

## 发音标准化

数字、产品名、英文术语的发音标准化仍由项目内置的 `normalize_with_ledger` 函数处理，在 `narration_plan.json` 生成时已完成。TTS 合成时直接使用 `spoken_text` 字段，不做二次修改。

`pronunciation_ledger.json` 记录所有发音标准化决策，TTS 前需确认该文件存在。

## 比例估算字幕说明

豆包 TTS 不提供字级时间戳（WordBoundary），因此字幕使用比例估算：

- **优点**：字幕文字 100% 准确（直接用写稿文案，不是语音识别结果）
- **缺点**：字幕出现/消失时间是估算的，可能与实际朗读有 ±0.5 秒偏差
- **适用场景**：新闻类短视频，观众对字幕时间精度要求不高
- **缓解措施**：每段音频的时长是实际测量的（ffprobe），不是按字数估算的，所以段级时间是准确的；段内句子级时间按比例分配

如果未来需要更高精度的字幕，可以考虑：
1. 用豆包 STT（语音识别）对生成的音频做强制对齐
2. 或在 TTS 生成时请求带时间戳的输出（如果工具支持）

## 边界与约束

- 不调用任何外部 TTS API（Azure、Google、ElevenLabs 等）
- 不需要任何 API 密钥
- 使用 `audio_to_audio_plus` 语音克隆，参考音频固定为 `example-audio.mp3`
- 不修改生成的音频内容（只做格式归一化和静音填充）
- 不使用语音识别结果替换写稿文案作为字幕
- 每段音频单独合成，不合并
- 音色描述固定，不随 segment 变化
- 失败的 segment 不阻塞其他段，最后统一报告
- 不上传、不发布、不分享任何音频文件

## 失败处理

- `text_to_audio_plus` 调用失败 → 记录失败的 segment_id，重试 1 次，仍失败则报告
- 音频文件保存失败 → 检查目录权限和磁盘空间
- `finalize` 时报某段音频缺失 → 回到第 2 步补合成
- `finalize` 时音频时长异常（过短或过长）→ 检查该段的 `spoken_text` 是否正确，必要时重新合成
- 视频渲染时 manifest 读取失败 → 检查 `doubao_audio_manifest.json` 的 schema 是否正确
- 比例估算字幕时间偏差过大 → 这是已知限制，不视为失败；如需更高精度请参考上方"比例估算字幕说明"
