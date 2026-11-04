---
name: ai-brief-doubao-voice
description: Synthesize AI每日早报 v6 narration with Doubao text_to_audio_plus (fixed anime-girl voice profile, natural speed) in a few long takes of about 100 s each (never over 120 s), split between stories. The orchestrator removes the "内容由豆包AI生成" tag, cuts every take back into sentences, re-asks a take that cannot be cut, aligns and mixes automatically; edge-tts is the automatic fallback.
metadata:
  author: local-project
  version: "6.0.0"
---

# AI每日早报 v6 · 豆包配音（几段长录音，程序切句）

豆包一次最多能合成约 120 秒音频。一次读完一长段，音色和情绪比几十条短句拼起来更稳定，所以程序把整期旁白分成**尽量少的几段**（每段约 100 秒，上限 120 秒）。分段只在两条新闻之间切开：钩子和第一条头条一起读，结尾和最后一条新闻一起读，几段的长度尽量相等。

你只负责按清单一条一条合成并保存。去掉开头的“内容由豆包AI生成”、切回单句、对齐、混音都由程序完成。

## 什么时候需要你

编排器运行到 `voice` 阶段、使用 `--tts doubao`（默认）时，会打印 AGENT HANDOFF 并退出（退出码 3）。

## 步骤

1. 读 `outputs/YYYY-MM-DD/artifacts/tts_manifest.json`，看 `doubao.tool`（`text_to_audio_plus`）和 **`todo`** 列表（只列还缺的音频，通常 1–3 条）。
2. 对 `todo` 里**每一条**，按顺序**一次只发一条**：
   - 调用 `text_to_audio_plus`，把该条 `request` **原样**发送。它已经填好了固定音色描述和这一段的全部原文，格式为：`"{音色描述}"，内容如下："{这一段原文}"`，其中音色描述为：
     > 清脆元气的少女音，音调偏高，声音明亮活泼、带着少年感，语气俏皮可爱、尾音微微上扬，语速明快、节奏轻快跳跃，像动漫角色配音一样元气满满，播报新闻时英文单词用自然的英文发音读出，纯人声无背景音乐
   - 把返回的音频**原样**保存到该条 `output_path`（如 `assets/audio/raw/t01.wav`；重新请求的半段为 `t01-1.wav`、`t01-2.wav`）。mp3 或 m4a 也可以，程序会自动转换。
3. 规则：
   - 一条 todo 是**连读的一整段**（几条新闻，约 1–2 分钟）。不要拆开，不要和别的条合并，不增删字词。新闻之间的换行原样保留。英文和阿拉伯数字（GPU、RSA-260、552B）照原样读，不拆单字母，不念成中文数字。
   - 不要在请求里要求语速，也不要事后变速：成片就用自然语速。
   - 返回的音频开头会有“内容由豆包AI生成”，**不用处理**，程序会自动去掉。
   - **限流**：遇到“合成太频繁”等报错，**等 10 秒**再重试同一条，最多 3 次。不要并发，不要连发。
4. 全部保存后，重新运行同一条编排器命令。程序会把每段切回单句；如果某段切不开、没读完或明显漏读，它会把这一段拆成两半（尽量在两条新闻之间）重新交接，`todo` 里会出现 `t01-1`、`t01-2` 这样的半段。照同样方法逐条合成，再重新运行即可。半段还不行就继续对半拆，最后到单句。

## 豆包配音不可用时

重新运行时加 `--tts edge`（或 `--no-agent`），程序会自动用 Microsoft Edge 神经网络语音合成，并带逐词时间戳。默认声音是 `zh-CN-YunxiNeural`，可以用环境变量 `EDGE_TTS_VOICE` 或 `EDGE_TTS_RATE` 调整。

## 程序会自动做的事

- **分段**：每句按“字数重量”估算时长（中文一字算 1，英文约两个字母算 1，数字按读法，逗号算半拍停顿），再除以实测语速。段数 = 总估算时长 ÷ 100 秒（向上取整），然后在新闻之间选切点，让最长的一段尽量短；每段“估算时长 + 水印约 2.7 秒”乘以 1.15 的余量后不能超过 120 秒。一条新闻本身超过上限时，才在句子之间拆开。
- **实测语速**：每段切好后记下真实语速，存进 `outputs/_state/doubao_speech_rate.json`，下次分段取最近 10 段的中位数（还没有记录时用默认值 4.5）。
- **去水印**：每条豆包音频的开头结构是：静音（0–0.33 秒）→ “内容由豆包AI生成”（0.33–2.25 秒，每次都是同一段固定录音）→ 0.20–0.46 秒静音 → 正文。程序按 10 毫秒一帧算音量（RMS ≥300 算有声），找到第一个有声帧后，再找其后第一段 ≥0.12 秒的静音（标签内部的停顿最长只有 0.08 秒），从静音结束处开始保留。这段静音必须起于 1.6–3.0 秒之间，否则不切，自检报告会出现 `doubao_watermark` 警告。edge 的音频从不做这一步。想检查真实音频的切点：`python3 scripts/check_doubao_watermark.py 文件...`。
- **截断检查**：音频接近 120 秒（差不到 1 秒），或有声部分不到估算时长的一半，就认为没读完或漏读，这一段拆成两半重新请求。
- **切句**：在整段音频里找停顿，用动态规划按每句的预估长度选切点，切在停顿正中间，每句再检查语速是否合理（相对整段平均在 0.55–1.8 倍之间）。切不开时先放宽停顿长度重试一次，还不行就对半拆开重新请求，已切好的部分不受影响。切点、语速比、水印结果写在 `tts_manifest.json` 各段的 `split` / `watermark` 字段里，请求次数写在 `voice_manifest.json` 的 `doubao_takes` 字段里。
- **保留自然停顿**：切出来的每句都带着它那一半停顿，`voice_manifest.json` 记下每句开头和结尾的静音（`lead` / `tail`）。时间轴把同一段、同一条新闻里的句子原样拼回去，不再插入停顿；新闻之间如果原有停顿不足 0.40 秒（钩子之后 0.30 秒）就补足，只延长，不缩短。
- 转为 48 kHz 单声道 WAV，写 `artifacts/alignments/uNN.json`（edge 为原生逐词时间；豆包按词的读音长度在说话时段内分配）。
- 混音：说话时音乐比人声低约 18 dB，最终响度 −16 LUFS，真峰值 −1.5 dBTP。
- 断点续跑：已保存的音频不会重做。改了稿子的句子，其旧音频会自动删掉重配。

## 可调参数（`ai_morning_brief/config.py`）

- 每段目标时长：`DOUBAO_TAKE_TARGET_SECONDS`（默认 100，环境变量 `AI_DAILY_DOUBAO_TAKE_SECONDS`）；单次上限 `DOUBAO_TAKE_MAX_SECONDS`（120），余量 `DOUBAO_TAKE_SAFETY`（1.15）。
- 默认语速：`DOUBAO_SPEECH_RATE_DEFAULT`（4.5）。
- 水印：`DOUBAO_WATERMARK_RMS_THRESHOLD`（300）、`DOUBAO_WATERMARK_MIN_GAP`（0.12 秒）、`DOUBAO_WATERMARK_WINDOW`（1.6–3.0 秒）。
- 限流后的等待时间：`DOUBAO_RATE_LIMIT_WAIT_SECONDS`（默认 10）。
- 更换音色：改 `DOUBAO_TTS_VOICE_PROFILE`。
