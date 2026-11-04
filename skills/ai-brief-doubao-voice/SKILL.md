---
name: ai-brief-doubao-voice
description: Synthesize AI每日早报 v6 narration one sentence at a time with Doubao — text_to_audio_plus with the fixed voice profile (default) or audio_to_audio_plus voice cloning with example-audio.mp3. The orchestrator speeds up, trims, aligns and mixes automatically; edge-tts is the automatic fallback.
metadata:
  author: local-project
  version: "4.0.0"
---

# AI每日早报 v6 · 豆包逐句配音

v6 起**每句话一个音频文件**：画面切换、卡片高亮和字幕都按句精确对齐。你只负责逐条合成并保存，加速、裁剪、对齐、混音都由程序完成。

## 什么时候需要你

编排器运行到 `voice` 阶段、使用 `--tts doubao`（默认）时，会打印 AGENT HANDOFF 并退出（退出码 3）。

## 步骤（默认：文生语音 `text-to-audio`）

1. 读 `outputs/YYYY-MM-DD/artifacts/tts_manifest.json`，确认 `doubao.tool` 是 `text_to_audio_plus`。
2. 对 `units` 里**每一条**（每条只有一句）：
   - 工具：`text_to_audio_plus`
   - 请求：`"{doubao.voice}"。朗读以下文字："{spoken_text}"`——`doubao.voice` 是固定音色描述，**逐字使用**：
     > 年轻女性，声音温柔甜美，咬字清晰有节奏感，情绪明亮自然，像晨间新闻女主播，无背景音无杂音，纯人声
   - `spoken_text` 是**原文**：GPU、RSA-260、552B 等英文和阿拉伯数字照原样读，不拆单字母、不念成中文数字、不增删字词
   - 不要在请求里要求语速：音色描述只描述音色，程序会把每句统一加速 1.25 倍（音高不变）
   - 保存到这一条的 `output_path`（`assets/audio/raw/uNN.wav`，mp3/m4a 也可以，程序会自动转换）
3. 不要把多条合并成一个文件；某条失败就重试一次。
4. 全部完成后重新运行同一条编排器命令。

## 声音克隆（`--tts-mode voice-clone`）

`doubao.tool` 为 `audio_to_audio_plus` 时：`@音频1` = `doubao.reference_audio`（项目根目录 `example-audio.mp3`），prompt 用 `doubao.prompt_template`（把 `{spoken_text}` 换成该条文字），保存到 `output_path`。克隆模式不加速。

## 豆包配音不可用时

重新运行时加 `--tts edge`（或 `--no-agent`），程序会自动用 Microsoft Edge 神经网络语音（`zh-CN-YunxiNeural`，可用环境变量 `EDGE_TTS_VOICE`/`EDGE_TTS_RATE` 调整）合成，并带逐词时间戳。

## 程序会自动做的事

- 文生语音模式每句加速 `AI_DAILY_DOUBAO_TEMPO`（默认 1.25，ffmpeg atempo，音高不变）
- 转为 48 kHz 单声道 WAV，精确裁掉首尾静音（−42 dBFS 阈值，保留 40 ms）
- 写 `artifacts/alignments/uNN.json`（edge 为原生逐词时间，豆包为句内比例时间）
- 句间停顿：句内 0.14 s，换条 0.40 s，钩子后 0.30 s
- 混音：说话时音乐低于人声约 18 dB，最终 −16 LUFS / −1.5 dBTP

## 更换音色

文生语音：改 `ai_morning_brief/config.py` 的 `DOUBAO_TTS_VOICE_PROFILE`（清单、交接提示、本 skill 都从这里取）。声音克隆：替换项目根目录 `example-audio.mp3`。
