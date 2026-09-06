# AI每日早报音乐资产

本目录固定使用 Publication Podcast Studio `1.2.0+codex.20260824` 内置的
三段背景音乐，避免运行时依赖插件缓存路径或第三方下载：

- `opening.mp3` — opening 段，SHA-256 `aa72f0b47baa4ef4c41946f9558fbf0b416eb16ad57d1e568a5dbdda49993d40`
- `middle-loop.mp3` — 正文循环段，SHA-256 `cfd4b0271e1e64f20696ccf0adffd8387787ef935a4415a7798ee20cee1aecbc`
- `ending.mp3` — 片尾段，SHA-256 `231bfe7bc13ac3a2fb412cd6868946fb04ba292f4eb8d904ed9581683be298fd`

混音沿用插件的可审计思路：三段素材按视频场景定位，先将每段音乐与人声
匹配到 `0 ± 0.5 LU`，再用 30 ms attack / 350 ms release 的语音侧链轻压，
最多衰减 4 dB，避免辅音被遮蔽。最终输出为 48 kHz/16-bit PCM，目标
`-16 LUFS`、`≤ -1.5 dBTP`，拒绝低于 `-45 dBFS` 的有效音乐或新增削波。
每次运行都会写出 `artifacts/background-music.json`，记录素材哈希、段落
位置、实际增益、响度测量和质量门禁结果。
