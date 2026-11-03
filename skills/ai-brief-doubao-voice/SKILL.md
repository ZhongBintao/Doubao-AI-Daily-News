---
name: ai-brief-doubao-voice
description: Synthesize AI每日早报 v6 narration one sentence at a time with Doubao voice cloning (audio_to_audio_plus + example-audio.mp3). The orchestrator trims, aligns and mixes automatically; edge-tts is the automatic fallback.
metadata:
  author: local-project
  version: "3.0.0"
---

# AI每日早报 v6 · 豆包逐句配音

v6 起**每句话一个音频文件**：画面切换、卡片高亮和字幕都按句精确对齐。你只负责逐条合成并保存，裁剪、对齐、混音都由程序完成。

## 什么时候需要你

编排器运行到 `voice` 阶段、使用 `--tts doubao`（默认）时，会打印 AGENT HANDOFF 并退出（退出码 3）。

## 步骤

1. 读 `outputs/YYYY-MM-DD/artifacts/tts_manifest.json`。
2. 对 `units` 里**每一条**（每条只有一句）：
   - 工具：`audio_to_audio_plus`
   - `@音频1`：`doubao.reference_audio`（项目根目录 `example-audio.mp3`）
   - prompt：把 `doubao.prompt_template` 里的 `{spoken_text}` 换成这一条的 `spoken_text`
   - 保存到这一条的 `output_path`（`assets/audio/raw/uNN.wav`，mp3/m4a 也可以，程序会自动转换）
3. 不要把多条合并成一个文件；不要增删字词；某条失败就重试一次。
4. 全部完成后重新运行同一条编排器命令。

## 豆包配音不可用时

重新运行时加 `--tts edge`（或 `--no-agent`），程序会自动用 Microsoft Edge 神经网络语音（`zh-CN-YunxiNeural`，可用环境变量 `EDGE_TTS_VOICE`/`EDGE_TTS_RATE` 调整）合成，并带逐词时间戳。

## 程序会自动做的事

- 转为 48 kHz 单声道 WAV，精确裁掉首尾静音（−42 dBFS 阈值，保留 40 ms）
- 写 `artifacts/alignments/uNN.json`（edge 为原生逐词时间，豆包为句内比例时间）
- 句间停顿：句内 0.14 s，换条 0.40 s，钩子后 0.30 s
- 混音：音乐低于人声约 18 dB，说话时再压 4 dB，最终 −16 LUFS / −1.5 dBTP

## 更换音色

替换项目根目录 `example-audio.mp3`（10–30 秒、单人、无背景音、语速偏快有精神的口播最适合短视频），全片自动生效。
