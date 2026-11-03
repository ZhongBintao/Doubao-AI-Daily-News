# AI每日早报音乐资产

本目录固定使用 Publication Podcast Studio `1.2.0+codex.20260824` 内置的
三段背景音乐，避免运行时依赖插件缓存路径或第三方下载：

- `opening.mp3` — opening 段，SHA-256 `aa72f0b47baa4ef4c41946f9558fbf0b416eb16ad57d1e568a5dbdda49993d40`
- `middle-loop.mp3` — 正文循环段，SHA-256 `cfd4b0271e1e64f20696ccf0adffd8387787ef935a4415a7798ee20cee1aecbc`
- `ending.mp3` — 片尾段，SHA-256 `231bfe7bc13ac3a2fb412cd6868946fb04ba292f4eb8d904ed9581683be298fd`

v6 混音（`ai_morning_brief/audio_mix.py`）：opening 覆盖开头钩子，middle-loop 循环覆盖正文，
ending 覆盖结尾页，段间 0.8 秒交叉淡化。每段先对齐到人声有效响度下 18 dB，再用人声包络
（30 ms attack / 350 ms release）侧链额外压低 4 dB；换条时叠加轻微 whoosh；最后两遍 loudnorm
到 −16 LUFS、真峰值 ≤ −1.5 dBTP。报告写入 `artifacts/mix_report.json`。

这三段素材节奏偏慢，后续可替换为节奏更快、确认可在 TikTok/小红书商用的曲目（保持文件名即可）。
