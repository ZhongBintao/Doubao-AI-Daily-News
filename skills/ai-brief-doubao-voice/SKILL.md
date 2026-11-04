---
name: ai-brief-doubao-voice
description: Synthesize AI每日早报 v6 narration with Doubao in ONE request for the whole edition (smaller batches only if a recording cannot be cut) — text_to_audio_plus with the fixed voice profile (default) or audio_to_audio_plus voice cloning with example-audio.mp3. The orchestrator cuts each paragraph back into sentences, speeds up, trims, aligns and mixes automatically; edge-tts is the automatic fallback.
metadata:
  author: local-project
  version: "5.1.0"
---

# AI每日早报 v6 · 豆包配音（整期一次请求，程序切句）

豆包请求太频繁会报“合成太频繁”而被限流，但允许一次合成长音频，所以**默认整期一次请求**：从钩子到结尾的全部旁白（约 1000 字以内）合成一条，程序再把它切回单句。只有整期音频切不开时，才改为每段 ≤300 字的几次请求；300 字的段还切不开，才对该段逐句请求。你只负责按清单逐条合成并保存，**切回单句**、加速、裁剪、对齐、混音都由程序完成。

## 什么时候需要你

编排器运行到 `voice` 阶段、使用 `--tts doubao`（默认）时，会打印 AGENT HANDOFF 并退出（退出码 3）。

## 步骤

1. 读 `outputs/YYYY-MM-DD/artifacts/tts_manifest.json`，看 `doubao.tool`（默认 `text_to_audio_plus`）和 **`todo`** 列表（只列还缺的音频）。
2. 对 `todo` 里**每一条**，按顺序**一次只发一条**：
   - 文生语音（`text_to_audio_plus`）：把该条 `request` **原样**发送。它已经填好了固定音色描述和原文，格式为：`"{音色描述}"。朗读以下文字："{这一段原文}"`，其中音色描述为：
     > 年轻女性，声音温柔甜美，咬字清晰有节奏感，情绪明亮自然，像晨间新闻女主播，无背景音无杂音，纯人声
   - 声音克隆（`audio_to_audio_plus`）：`@音频1` = `doubao.reference_audio`（项目根目录 `example-audio.mp3`），prompt = 该条 `request`。
   - 保存到该条 `output_path`：整期或整段为 `assets/audio/raw/b01.wav`（小段为 `b01-1.wav` 等），单句为 `uNN.wav`。mp3 或 m4a 也可以，程序会自动转换。
3. 规则：
   - 一条 todo 是**连读的一整段**（默认就是整期全部旁白）。不要拆开，不要和别的条合并，不增删字词。英文和阿拉伯数字（GPU、RSA-260、552B）照原样读，不拆单字母，不念成中文数字。
   - 不要在请求里要求语速。程序会统一加速 1.25 倍，音高不变（只对文生语音）。
   - **限流**：遇到“合成太频繁”等报错，**等 10 秒**再重试同一条，最多 3 次。不要并发，不要连发。
4. 全部保存后，重新运行同一条编排器命令。程序会把每段切回单句；如果没能自动切开，它会重新交接：整期切不开时 `todo` 变成几段 ≤300 字的小段；小段还切不开，变成该段的**单句**（`kind: "sentence"`）。照同样方法逐条合成，再重新运行即可。

## 豆包配音不可用时

重新运行时加 `--tts edge`（或 `--no-agent`），程序会自动用 Microsoft Edge 神经网络语音合成，并带逐词时间戳。默认声音是 `zh-CN-YunxiNeural`，可以用环境变量 `EDGE_TTS_VOICE` 或 `EDGE_TTS_RATE` 调整。

## 程序会自动做的事

- **切句**：在整段音频里找停顿，按每句的字数（英文按音节、数字按读法估算）预估每个句界的位置，在最近的停顿中间切开。每句切出来后检查语速是否合理。切不准时先放宽停顿长度重试一次；还不行，整期改为几段 ≤300 字的小段请求，小段再不行才逐句请求，已切好的部分不受影响。切点和检查结果写在 `tts_manifest.json` 各段的 `split` 字段里，请求次数写在 `voice_manifest.json` 的 `doubao_requests` 字段里。
- 文生语音模式每句加速 `AI_DAILY_DOUBAO_TEMPO`（默认 1.25，用 ffmpeg atempo，音高不变）。
- 转为 48 kHz 单声道 WAV，精确裁掉首尾静音（阈值 −42 dBFS，保留 40 ms）。
- 写 `artifacts/alignments/uNN.json`（edge 为原生逐词时间，豆包为句内比例时间）。
- 句间停顿统一由程序控制：句内 0.14 s，换条 0.40 s，钩子后 0.30 s。
- 混音：说话时音乐比人声低约 18 dB，最终响度 −16 LUFS，真峰值 −1.5 dBTP。
- 断点续跑：已保存的音频不会重做。改了稿子的句子，其旧音频会自动删掉重配。

## 可调参数（`ai_morning_brief/config.py`）

- 一次请求的字数上限：`DOUBAO_BATCH_MAX_CHARS`（默认 1500，即整期一次；环境变量 `AI_DAILY_DOUBAO_BATCH_CHARS`，设为 300 就是每段一次）；切不开时的小段上限：`DOUBAO_BATCH_FALLBACK_CHARS`（300）。
- 限流后的等待时间：`DOUBAO_RATE_LIMIT_WAIT_SECONDS`（默认 10）。
- 更换音色：文生语音改 `DOUBAO_TTS_VOICE_PROFILE`；声音克隆替换项目根目录的 `example-audio.mp3`。
