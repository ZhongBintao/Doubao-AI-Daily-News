# AI Daily News Studio Codex Plugin

`AI Daily News Studio`（中文：`AI每日早报`）是一个面向 Codex 的内容生产插件，
用于把 AIHOT 过去 24 小时的精选资讯，制作成可审计的中文 AI 资讯视频和私有发布整合包。

## 能做什么

- 从 AIHOT 冻结精选资讯和来源链接。
- 生成来源绑定的编辑计划、卡片文案、旁白和字幕。
- 使用 Azure Speech（默认）或明确指定的 Gemini TTS。
- 完成人声对齐、背景音乐混音、OpenMontage/HyperFrames 渲染和质量门禁。
- 可选生成 16:9、3:4、9:16 封面及发布文案整合包。
- 使用 Codex 内置浏览器采集原文视觉素材，并保留来源、哈希和捕获审计信息。

## 安装与使用

本仓库根目录包含 `.codex-plugin/plugin.json`，可通过 Codex 插件管理界面安装。
安装后可以直接使用插件中的默认任务，例如：

```text
生成今天的 AI每日早报完整私有发布包。
```

完整工作流入口是 `ai-daily-news-studio-workflow`。项目运行主入口为：

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline prepare \
  --date YYYY-MM-DD \
  --env-file .env \
  --source-visual-mode off
```

正式视频运行前，需要先完成编辑计划；生产运行示例：

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline run \
  --date YYYY-MM-DD \
  --force \
  --reuse-source \
  --env-file .env \
  --speech-provider azure
```

## 原文截图落盘

截图采集固定使用 Codex 内置浏览器，不连接 Chrome，不使用 Terminal、base64 或剪贴板
传输浏览器图片字节。

当前插件提供隔离的 [`browser_screenshot_capture.mjs`](skills/ai-daily-news-studio-workflow/scripts/browser_screenshot_capture.mjs)：

- 直接接收 `tab.screenshot()` 返回的原始 `Uint8Array`。
- 校验 PNG/JPEG 格式并记录真实尺寸、格式和 SHA-256。
- 只允许写入显式声明的绝对 `workspaceRoots`。
- 拒绝路径穿越、符号链接逃逸和覆盖已有文件。
- 原子写入截图，并生成 `capture.json` 供流水线审计。
- 不修改既有 `tab.screenshot()` API，也不改变其他浏览器功能。

原文视觉协议见 [`x-screenshot-capture.md`](skills/ai-daily-news-studio-workflow/references/x-screenshot-capture.md)。
完整自动验收使用：

```bash
OpenMontage/.venv/bin/python -m ai_morning_brief.pipeline prepare \
  --date YYYY-MM-DD \
  --env-file .env \
  --source-visual-mode auto \
  --source-visual-min-stories 1
```

旧的 `unavailable` 截图请求是终态；修复后必须重新 `prepare`，不能重复尝试同一个已终止请求。

## 目录结构

```text
.codex-plugin/plugin.json                         插件清单
skills/                                           发布版 Codex skills
skills/ai-daily-news-studio-workflow/             主工作流 skill
ai_morning_brief/                                  AIHOT、编辑、音频和渲染代码
outputs/YYYY-MM-DD/                               冻结快照、审计文件和视频产物
```

## 测试

截图 helper：

```bash
node skills/ai-daily-news-studio-workflow/scripts/test_browser_screenshot_capture.mjs
```

完整离线测试：

```bash
python3 -m unittest discover -s ai_morning_brief/tests -v
```

测试不得自动请求 AIHOT、Azure、Gemini 或其他外部服务。不要提交 `.env`、API key、Cookie、
浏览器 Profile 或授权请求头。

## 版本

当前插件版本：`0.5.1+codex.20260905105020`
