import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  preflightWorkspace,
  saveBrowserScreenshot,
  writeCaptureReceipt,
} from "./browser_screenshot_capture.mjs";

const root = await mkdtemp(join(tmpdir(), "ai-daily-news-screenshot-"));
try {
  const inbox = join(root, "source-visuals", "raw", "item-1");
  const raw = Uint8Array.from([
    0xff, 0xd8,
    0xff, 0xc0, 0x00, 0x11, 0x08, 0x00, 0x10, 0x00, 0x20,
    0x03, 0x01, 0x11, 0x00, 0x02, 0x11, 0x00, 0x03, 0x11, 0x00,
    0xff, 0xd9,
  ]);

  const preflight = await preflightWorkspace({
    outputDirectory: inbox,
    workspaceRoots: [root],
    fileNameStem: "viewport",
    extension: "jpg",
  });
  assert.equal(preflight.status, "available");

  const saved = await saveBrowserScreenshot({
    raw,
    outputDirectory: inbox,
    workspaceRoots: [root],
    fileNameStem: "viewport",
    capturedUrl: "https://example.com/item-1",
    viewport: { x: 0, y: 0, width: 1440, height: 900 },
    viewportOverride: { x: 0, y: 0, width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  assert.equal(saved.format, "jpeg");
  assert.deepEqual(saved.originalDimensions, { width: 32, height: 16 });
  assert.deepEqual([...await readFile(saved.path)], [...raw]);

  await writeCaptureReceipt({
    outputDirectory: inbox,
    workspaceRoots: [root],
    receipt: {
      complete: true,
      asset_count: 1,
      source_url: saved.capturedUrl,
      capture_executor: "codex_in_app_browser",
      capture_method: "iab-expanded-viewport-screenshot",
      capture_contract_id: "iab-expanded-viewport-v1",
      files: [{ file: "viewport.jpg", ...saved }],
    },
  });
  assert.match(await readFile(join(inbox, "capture.json"), "utf8"), /"complete": true/);

  await assert.rejects(
    () => saveBrowserScreenshot({ raw, outputDirectory: inbox, workspaceRoots: [root], fileNameStem: "viewport" }),
    (error) => error.code === "destination_exists",
  );
  const outside = await preflightWorkspace({
    outputDirectory: join(root, "..", "outside"),
    workspaceRoots: [root],
  });
  assert.equal(outside.status, "unavailable");
  assert.equal(outside.errorCode, "path_outside_workspace");
} finally {
  await rm(root, { recursive: true, force: true });
}

console.log("browser screenshot capture helper: ok");
