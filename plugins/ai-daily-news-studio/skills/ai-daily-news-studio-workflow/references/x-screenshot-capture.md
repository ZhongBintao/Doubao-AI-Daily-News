# 原文视觉素材采集协议 v5

只处理当天冻结的原文 URL。自动模式固定使用 Codex 内置浏览器；不连接
Chrome，不登录，不读取 Cookie、Local Storage、Profile、密码或授权头。

## 自动化能力预检与终止规则

打开第一条 URL 之前，当前任务必须同时具备：内置浏览器控制、直接项目文件写入、
以及 1440×900 扩大视口截图能力。将检查结果写入
`artifacts/source_visual_preflight.json`；三项任一不可用就把对应请求终止为
`unavailable`，使用 `error_code=browser_capture_unavailable`，并返回
`awaiting_screenshots`（最低截图要求大于 0 时）。禁止用 CUA 操作 Terminal、切换
Chrome、base64/剪贴板中转、复制浏览器状态或再次尝试。每个冻结原文最多一次尝试。
状态只能是 `pending → validated` 或 `pending → unavailable`。

## 捕获

1. 复用一个可见的内置浏览器标签页，并在打开原文前通过 `viewport` 能力临时设置
   `{"width": 1440, "height": 900}`；等待正文、帖子或媒体元素稳定。
2. 截图完成后立即调用 `viewport.reset()`，不把临时尺寸带到后续浏览器任务。
3. X 只捕获原帖主体；排除回复、他人转发、引用卡片和推荐流。冻结链接是
   转发或引用帖时，跟随可验证的原帖并记录 canonical URL。
4. 普通网页直接截取扩大后的当前响应式视口，视口中应包含站点身份、文章标题和相邻
   首段或主图；禁止全文长图和拼接。正文存在明确相关图片时，可额外保存最多两张
   图片元素截图；找不到时跳过。自动模式应积极处理清单中的每一条可访问原文。
5. 截图记录真实的 viewport override、device scale 和原始像素尺寸；不要求伪造
   2× PNG。视频保留
   原始 MP4/MOV/WebM。不做本地裁切、缩放或重编码，展示文件逐字节复制原始文件。
6. 保留识别站点身份所需的必要结构，排除无关页面外壳、登录面板、广告、二维码、评论区和推荐流。X 每条
   故事最多一个素材；普通网页最多一个首屏素材和两张正文图片。
7. `capture.json` 记录 `capture_type`、`asset_role`、`viewport`、`viewport_override`、
   `capture_contract_id`、
   `device_scale_factor`、`crop_box`、`original_dimensions`、
   `evidence_text`、`capture_attempt`、来源 URL 和捕获方式；网站主内容还记录
   `content_bounds`。请求和结果清单还必须记录 `attempts`、`capture_executor`、
   `terminal_state` 和 `error_code`。

最小截图调用顺序固定为：

```js
const viewport = await iab.capabilities.get("viewport");
await viewport.set({width: 1440, height: 900});
const raw = await tab.screenshot({fullPage: false});
await viewport.reset();
```

`tab.screenshot()` 的返回值必须在同一个 Codex Node REPL 会话中直接写入工作区，不能经过
Terminal、Chrome、base64 或剪贴板。插件随附的
`scripts/browser_screenshot_capture.mjs` 是隔离的落盘 helper；它不修改既有的
`tab.screenshot()`，只接受原始 `Uint8Array`，校验 PNG/JPEG、计算 SHA-256，并在声明的
`workspaceRoots` 内用拒绝覆盖的原子方式写入图片和 `capture.json`。

调用时必须传入任务工作区的绝对路径；不要使用未解析的 `~`、相对路径或工作区外路径：

```js
const captureFile = await import("/ABSOLUTE/PATH/TO/skills/ai-daily-news-studio-workflow/scripts/browser_screenshot_capture.mjs");
const check = await captureFile.preflightWorkspace({
  outputDirectory: "/ABSOLUTE/WORKSPACE/outputs/YYYY-MM-DD/source-visuals/raw/ITEM_ID",
  workspaceRoots: ["/ABSOLUTE/WORKSPACE"],
  fileNameStem: "viewport",
  extension: "jpg",
});
if (check.status !== "available") throw new Error(`${check.errorCode}: ${check.error}`);

// 复用上一步刚捕获的 raw；不要再次调用 tab.screenshot()。
const frozenOriginalUrl = request.original_url;
const saved = await captureFile.saveBrowserScreenshot({
  raw,
  outputDirectory: "/ABSOLUTE/WORKSPACE/outputs/YYYY-MM-DD/source-visuals/raw/ITEM_ID",
  workspaceRoots: ["/ABSOLUTE/WORKSPACE"],
  fileNameStem: "viewport",
  capturedUrl: await tab.url(),
  viewport: {x: 0, y: 0, width: 1440, height: 900},
  viewportOverride: {x: 0, y: 0, width: 1440, height: 900},
  deviceScaleFactor: 2,
  cropBox: {x: 0, y: 0, width: 1440, height: 900},
  evidenceText: "截图中可见并与新闻 claim 对应的原文",
});
await captureFile.writeCaptureReceipt({
  outputDirectory: "/ABSOLUTE/WORKSPACE/outputs/YYYY-MM-DD/source-visuals/raw/ITEM_ID",
  workspaceRoots: ["/ABSOLUTE/WORKSPACE"],
  receipt: {
    complete: true,
    asset_count: 1,
    source_url: frozenOriginalUrl,
    captured_url: saved.capturedUrl,
    capture_executor: "codex_in_app_browser",
    capture_method: "iab-expanded-viewport-screenshot",
    capture_contract_id: "iab-expanded-viewport-v1",
    capture_type: "viewport_screenshot",
    files: [{file: saved.path.split("/").pop(), asset_role: request.capture_scope === "original_post_only" ? "x_original_post" : "article_main_content", ...saved}],
  },
});
```

如果 helper 的预检失败，必须记录 `errorCode` 并停止；不能回退到旧的 Terminal、Chrome、
base64 或剪贴板流程。目标文件和 `capture.json` 已存在时会返回
`destination_exists`，也不能覆盖或重试同一冻结 URL。

```json
{
  "complete": true,
  "asset_count": 1,
  "source_url": "https://example.com/post",
  "capture_executor": "codex_in_app_browser",
  "capture_method": "iab-expanded-viewport-screenshot",
  "capture_contract_id": "iab-expanded-viewport-v1",
  "capture_scope": "original_post_only",
  "asset_role": "x_original_post",
  "capture_type": "viewport_screenshot",
  "viewport": {"width": 1440, "height": 900},
  "viewport_override": {"width": 1440, "height": 900},
  "device_scale_factor": 2,
  "crop_box": {"x": 0, "y": 0, "width": 1440, "height": 900},
  "original_dimensions": {"width": 1440, "height": 900},
  "evidence_text": "截图中可见并与新闻 claim 对应的原文",
  "capture_attempt": 1
}
```

## 本地验收与展示

schema 5 仍以自然尺寸记录展示信息；采集时统一请求 1440×900 的扩大视口，但仍记录
浏览器实际返回的尺寸。扩大视口截图由模板按展示舞台自然放大；不再启动 clip 或分辨率重捕循环。原始文件保留在 raw 目录；
展示副本逐字节复制，并记录 `presentation_sha256`、`presentation_dimensions`
和 `presentation_format`。

素材必须通过 `evidence_text` 映射到 story claim。v5 允许后置
`visual_asset_id` beats：X 最多一个，普通网页最多三个，先显示资讯卡，再按
beat 顺序在同一 `source-visual-layer` 直接叠放图片或静音视频。素材位于卡片和故事标题
之上、顶部导航/字幕之下；不隐藏卡片、不按 card IDs 做复杂交叉淡入、
不渲染来源徽章。
