# AI每日早报音乐资产

本目录固定使用 Publication Podcast Studio `1.2.0+codex.20260824` 内置的
三段背景音乐，避免运行时依赖插件缓存路径或第三方下载：

- `opening.mp3` — opening 段，SHA-256 `aa72f0b47baa4ef4c41946f9558fbf0b416eb16ad57d1e568a5dbdda49993d40`
- `middle-loop.mp3` — 正文循环段，SHA-256 `cfd4b0271e1e64f20696ccf0adffd8387787ef935a4415a7798ee20cee1aecbc`
- `ending.mp3` — 片尾段，SHA-256 `231bfe7bc13ac3a2fb412cd6868946fb04ba292f4eb8d904ed9581683be298fd`

v6 混音（`ai_morning_brief/audio_mix.py`，2026-11-04 按新版模板重新编排）：

| 段落 | 素材特点（实测） | 放置方式 |
|---|---|---|
| opening（15 s） | 第 1–6 秒最响，之后衰减 | 第 0 帧起铺在钩子下；第一条头条出现后 2 秒内淡出 |
| middle-loop（220 s） | 很安静（约 −41 dBFS）、约 60 BPM，首秒有一个重音 | 第一条头条出现时起播，首拍重音落在转场上；覆盖整个正文（220 秒以内不会循环，更长时带 1.5 秒交叉淡化循环） |
| ending（6.9 s） | 越往后越响，收尾在最后 2 秒 | 尾部对齐：最后一个音落在视频最后一帧，前段安静部分压在最后一条新闻尾声下；结尾页 ≥5.5 秒 |

每段增益按整首曲子的响度计算（不随截取长度变化），音乐底噪低于人声有效响度 14 dB，
说话时用人声包络（30 ms attack / 350 ms release）侧链再压 4 dB，合计约 18 dB；换条时叠加
程序合成的轻微 whoosh；最后两遍 loudnorm 到 −16 LUFS、真峰值 ≤ −1.5 dBTP。报告写入
`artifacts/mix_report.json`（`alignment` 字段记录对齐情况）。

替换曲目时保持文件名即可；混音会按新曲子的整首响度自动对齐。
