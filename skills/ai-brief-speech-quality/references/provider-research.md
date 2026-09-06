# Azure / Gemini TTS 调研记录

## 结论

Azure `zh-CN-Xiaochen:DragonHDLatestNeural` 继续作为默认生产声道。当前实现
把 `spoken_text` 作为唯一 TTS 输入，优先使用 Speech SDK 的 WordBoundary 回调，
并把 REST + STT 路径降级标记为兼容诊断。Gemini 默认只做盲测；用户明确传入
`--speech-provider gemini` 时可生成一次性 Google TTS 版本，且不会自动切换或
改变日常默认 provider。

## Azure

- [Azure HD voices](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/high-definition-voices)
  说明 DragonHD 的 voice 格式、temperature 参数和 WordBoundary 事件；
  DragonHD 不支持 `<prosody>`，所以本项目发送中性速率/音高并使用模板
  `temperature=0.7`，不再用旧模板的速率/音高控制。
- [Azure pronunciation SSML](https://learn.microsoft.com/en-ca/azure/ai-services/speech-service/speech-synthesis-markup-pronunciation)
  提供 `phoneme`、`sub`、`say-as` 和自定义 lexicon。生产稿件先用本地
  `normalize_with_ledger` 改写高风险 token（例如 `14 tokens/s`、`Q4_K_M`），
  只有需要逐词音素控制时才扩展到 SSML pronunciation 元素。

## Gemini

- [Gemini speech generation](https://ai.google.dev/gemini-api/docs/speech-generation)
  当前支持单人/双人 TTS、自然语言控制语气和节奏，明确列出普通话，并将
  `gemini-3.1-flash-tts-preview` 列为可用模型；输出是 audio-only，当前示例
  使用 Interactions API 的 `response_format: {type: "audio"}`。
- 该文档没有提供与 Azure WordBoundary 等价的稳定逐词时间戳契约（这是基于
  文档缺项的工程推断，不是对 Gemini 内部能力的断言）。因此 Gemini 生成的
  音频暂不能直接替代本项目的字幕对齐来源。

## 盲测门禁

`artifacts/tts-benchmark/benchmark.json` 固定 20 条短句，覆盖实际稿件和
`tokens/s`、存储单位、量化代码、locale、版本号等回归项。A/B 标签与 provider
映射分离保存；比较时至少需要：关键数字错误为 0、术语准确率 ≥ 0.95、
自然度差值不低于 -0.25，并且仍需人工明确批准。没有 `GEMINI_API_KEY` 时，
状态为 `awaiting_gemini_credentials`，不影响 Azure 成片。一次性 Gemini 成片
会写 `google_audio_manifest.json`，并明确标注比例字幕对齐而非原生词边界。
