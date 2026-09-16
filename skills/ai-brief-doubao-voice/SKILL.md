---
name: ai-brief-doubao-voice
description: Synthesize AI每日早报 narration audio with Doubao in-conversation TTS (text_to_audio_plus + fixed "温柔甜美" voice profile, timbre only — no tempo instruction; audio_to_audio_plus voice cloning as fallback), ASR-measured subtitle timing with per-block proportional fallback, OpenMontage loudness mixing, and a 1.25x final-cut speed-up applied after rendering.
metadata:
  author: local-project
  version: "3.1.0"
---

# AI早报语音合成（豆包文生语音版）

v3.0 把生产默认切回 `text_to_audio_plus` + 固定音色描述；`audio_to_audio_plus` 语音克隆保留为显式回退。字幕从"比例估算"升级为"本地 ASR 实测时间戳（失败段按块回退比例）"。

v3.1：音色描述只描述音色、**不含语速**（语速指令会让音色不稳定），成片节奏改由渲染后统一加速 1.25x 实现。

## 模式对照

| 维度 | v1.x 豆包描述式 | v2.0 豆包语音克隆 | v3.0 豆包文生语音（当前默认） |
|------|----------------|------------------|------------------------------|
| TTS 工具 | `text_to_audio_plus` | `audio_to_audio_plus` | `text_to_audio_plus` |
| 音色 | 固定文字描述 | 参考音频 `example-audio.mp3` | 固定文字描述（仅音色，不含语速） |
| 参考音频 | 不需要 | 必需 | 不需要 |
| 字幕对齐 | 比例估算 | 比例估算 | 本地 ASR 实测时间戳（回退比例） |
| provider | `doubao` | `doubao-voice-clone` | `doubao` |
| 合成文本 | `spoken_text` | `spoken_text` | `spoken_text`（= 屏幕原文） |

两种模式共用同一套代码与流水线，`config.py` 的 `DEFAULT_TTS_MODE` 决定默认值；命令行可用
`--tts-mode voice-clone` / `--speech-provider doubao-voice-clone` 单次覆盖。

## 固定音色描述（text-to-audio 模式）

所有 segment 的 TTS 生成必须使用以下描述，**逐字使用、不修改**（单一来源见
`ai_morning_brief/config.py` 的 `DOUBAO_TTS_VOICE_PROFILE`）：

> 年轻女性，声音温柔甜美，咬字清晰有节奏感，情绪明亮自然，像晨间新闻女主播，无背景音无杂音，纯人声

描述里**不要加入语速指令**：实测表明提示词调节语速会让音色变得不稳定。如需微调音色，只修改 `config.py` 中的这一个常量；`tts_manifest.json`、交接指令和本文件都引用同一处，不会出现三处不一致。

### 成片语速（1.25x，渲染阶段统一加速）

成片节奏不靠 TTS 提示词，而是在**渲染完成后对整条成片提速 1.25 倍**（`config.py` 的 `DEFAULT_OUTPUT_TEMPO`）：

- 位置：HyperFrames 渲染出成片之后、质量检测之前，因此质量报告里的时长与哈希都是最终交付文件
- 实现：ffmpeg `setpts=PTS/1.25,fps=30` + `atempo=1.25`——画面与音频一起加速，**音高保持不变**
- 烧录字幕随画面自动同步；外挂 `assets/subtitles/subtitles.srt` 的时间戳由流水线按同一倍率重写
- 如需按自然速度交付：`export AI_BRIEF_OUTPUT_TEMPO=1.0`
- 加速失败时**不会**丢弃已渲染的成片：降级交付 1.0x，并在 `quality_report.json` 的 `checks.output_tempo` 与终端日志中记录失败原因

## 完整流程

### 第 1 步：生成 TTS 待合成清单

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.doubao_tts_adapter prepare --date YYYY-MM-DD
```

输出 `artifacts/tts_manifest.json`，每个 segment 包含：

- `segment_id`（如 `intro`、`story-01`、`outro`）
- `display_text` / `spoken_text`（**两者恒等**：v3.0 起不再做朗读改写）
- `char_count`、`output_path`（音频保存路径）
- `minimum_duration_seconds`（最低时长要求，如 overview 段要求 5 秒）

### 第 2 步：逐段合成音频（text-to-audio 默认模式）

对清单中的每个 segment 调用豆包 `text_to_audio_plus`：

- **音色描述**：`tts_manifest.json` 的 `voice` 字段（固定文案，见上）
- **请求格式**：`"{音色描述}"。朗读以下文字："{spoken_text}"`
- **输出格式**：WAV，保存到清单中 `output_path` 指定路径

注意：
- 每段单独合成，不合并多段文字为一次调用
- 朗读文本就是 `spoken_text` 原文：`GPU`、`RSA-260`、`552B`、`2020` 等英文缩写、型号代码和阿拉伯数字**照原样读**，不要拆成单字母、不要念成中文数字
- 不增删字词；不要对音频做任何后期处理（降噪、变速、音量调整），finalize 会统一处理
- 某段合成失败时记录失败 segment_id，继续其余段落，最后统一报告

如果该次运行使用回退模式（`tts_mode=voice-clone`），改用 `audio_to_audio_plus`：
`@音频1` = 项目根目录 `example-audio.mp3`，
prompt 为 `"用参考音频的音色、语速和朗读风格，清晰朗读以下文字，不增删字词，无背景音无杂音：{spoken_text}"`。

### 第 3 步：检查合成进度

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.doubao_tts_adapter status --date YYYY-MM-DD
```

显示哪些 segment 已有音频、哪些缺失。全部完成后进入第 4 步。

### 第 4 步：归一化与生成 manifest

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.doubao_tts_adapter finalize --date YYYY-MM-DD
```

finalize 自动完成：

1. 验证所有 segment 音频存在且非空
2. 用 ffmpeg 统一转为 48kHz / 16bit / mono WAV
3. 短段落用尾部静音填充到 `minimum_duration_seconds`
4. 读取每段实际时长
5. 生成 `artifacts/doubao_audio_manifest.json`：
   - `provider`: `"doubao"`（text-to-audio）/ `"doubao-voice-clone"`
   - `voice` / `voice_profile`: 固定音色描述
   - `alignment_provider`: `"doubao-proportional"` / `"doubao-voice-clone-proportional"`
   - `native_word_boundary`: `false`

（成片语速不在这里处理，见"成片语速"章节。）

### 第 5 步：视频渲染（复用音频）

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline run \
  --date YYYY-MM-DD --force --reuse-source --reuse-audio \
  --env-file .env --speech-provider doubao
```

pipeline 自动完成：

1. 读取 `doubao_audio_manifest.json`，校验每段 `spoken_text` 与音频哈希
2. **本地 ASR 转写各段旁白**（faster-whisper / mlx-whisper，增量缓存按音频哈希）并写入
   `artifacts/alignments/<segment_id>.json`（词级时间戳）
3. 字幕用真实词级时间轴排布；文字 100% 来自写稿文案（`display_text`），ASR 只提供时间
4. 背景音乐混音 → HTML 模板物化 → HyperFrames 渲染 → 质量门禁

**转写失败不阻塞渲染**：无 ASR 引擎、引擎报错或单段失败的块自动回退为比例估算（该段文本仍 100% 准确）。

### 第 6 步：质量审核

- `artifacts/doubao_audio_manifest.json` — 各段时长、音色描述、语速设置
- `artifacts/loudness`/`background-music.json` — 混音报告：预闪避间隙 ±0.5 LU、
  sidechain 30/350ms、衰减 ≤4 dB、整片 ≈ -16 LUFS、真峰值 ≤ -1.5 dBTP、无新增削波
- `artifacts/quality_report.json` — 整体质量门禁 = `pass`
- `assets/subtitles/subtitles.srt` — 字幕文字抽查
- `renders/ai-daily-news-YYYY-MM-DD.mp4` — 最终视频

## 发音与朗读口径

v3.0 起 `spoken_text` 恒等于 `display_text`（屏幕原文）：数字、缩写、英文术语不再做朗读改写，由语音模型直接朗读，避免历史上"GPU 读成 G P U、RSA-260 读成 R S A 二百六十"导致的断续与错读。

`pronunciation_ledger.json` 仍会生成，用于记录标准化决策（现在只做空白与千位分隔符规范化）。

## 字幕时间戳说明

- 字幕出现/消失时间来自**本地 ASR 对实际音频的词级转写**，与配音同步
- 字幕文字始终是写稿文案，ASR 只贡献时间；个别段落定位失败时用相邻段落边界插值
- 整段无法转写时，该段回退为比例估算（段时长按字数分配），行为与 v2.0 相同

## 边界与约束

- 不调用任何外部 TTS API（Azure、Google、ElevenLabs 等），不需要 API 密钥
- 默认使用 `text_to_audio_plus` + 固定音色描述；语音克隆为显式回退
- 不修改生成的音频内容（只做格式归一化与静音填充）；节奏只在渲染完成后对成片统一加速
- 不使用语音识别结果替换写稿文案作为字幕
- 每段音频单独合成，不合并；音色描述固定，不随 segment 变化
- 失败的 segment 不阻塞其他段，最后统一报告

## 失败处理

- TTS 调用失败 → 记录 segment_id，重试 1 次，仍失败则报告并等待用户决定
- `finalize` 报某段音频缺失 → 回到第 2 步补合成
- 某段时长异常（过短/过长）→ 检查该段 `spoken_text`，必要时重新合成
- 成片节奏不理想 → 调整 `DEFAULT_OUTPUT_TEMPO`（1.0–2.0），或 `export AI_BRIEF_OUTPUT_TEMPO=1.0` 按自然速度交付
- 字幕与配音偏差明显 → 检查该段 `artifacts/alignments/<segment_id>.json` 是否存在；缺失的段属正常比例回退
