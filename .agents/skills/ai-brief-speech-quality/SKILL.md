---
name: ai-brief-speech-quality
description: Verify AI每日早报 pronunciation, Azure WordBoundary alignment, equal-loudness mixing, and the gated Gemini shadow benchmark.
metadata:
  author: local-project
  version: "0.4.0"
---

# AI早报语音质量

Use this skill after a validated narration plan exists and before a private
video is reported. Azure Xiaochen DragonHD remains the production default.

## Provider rules

- Send only `spoken_text` to Azure. Use the Speech SDK WordBoundary callback as
  the canonical timing source; never replace approved captions with Azure STT
  guesses. DragonHD receives neutral rate/pitch and template `temperature: 0.7`;
  avoid unsupported prosody/style controls.
- If the SDK is unavailable, the REST/STT compatibility path must be labelled
  in the manifest and is a temporary diagnostic, not a silent provider change.
- Gemini TTS (`gemini-3.1-flash-tts-preview`) is benchmark-only on the default
  Azure path. For an explicitly requested one-off edition, pass
  `--speech-provider gemini`; the pipeline reads `GOOGLE_AI_STUDIO_API_KEY`,
  records `google_audio_manifest.json`, and labels deterministic proportional
  subtitle timing because Gemini has no Azure-equivalent WordBoundary output.
  There is no automatic provider fallback. Generate the 20-phrase blind set
  when `GEMINI_API_KEY` exists on an Azure run, score numeric correctness, term
  accuracy, and naturalness, and retain explicit approval for any future
  default-routing change. Missing credentials produce `awaiting_gemini_credentials`.

## Editorial and audio gate

Before TTS, require `artifacts/editorial_quality_report.json=pass`. Authored
caption units are complete short sentences and are timed as units; do not
mechanically split them at commas or replace them with recognition output.

## Subtitle alignment gate

Every Azure production run, including `--reuse-audio`, must build subtitles
from the existing `artifacts/alignments/{segment_id}.json` WordBoundary data
when alignment is enabled. Missing or empty alignment files must stop the run;
they must never trigger a silent proportional fallback. Keep the reviewed
`display_text`/caption-unit text unchanged and use alignment data only for
timestamps. `--no-align` is an explicit proportional fallback, while Gemini
is always labelled `gemini-proportional`/approximate. Before accepting a run,
check `checks.speech.subtitle_alignment` in `quality_report.json`: Azure
should report `azure-word-boundary` (or an explicitly labelled compatibility
mode), and `proportional_fallback_segments` must be empty unless the fallback
was explicitly requested.

Run the local tests before any provider call. The mix report must show a
per-section voice/music pre-duck gap within ±0.5 LU, sidechain attack/release
of 30/350 ms with no more than 4 dB attenuation, final loudness near −16 LUFS,
true peak at or below −1.5 dBTP, and no newly clipped samples. Keep voice,
music, and final-mix stems for audit.

Review `artifacts/pronunciation_ledger.json`,
`artifacts/azure_audio_manifest.json`,
`artifacts/tts-benchmark/benchmark.json`, and
`artifacts/background-music.json` before accepting a run. Never write keys,
cookies, or provider headers to artifacts.
