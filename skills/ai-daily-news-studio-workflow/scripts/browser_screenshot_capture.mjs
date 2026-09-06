import { createHash, randomUUID } from "node:crypto";
import { link, lstat, mkdir, open, realpath, unlink } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

export const CAPTURE_EXECUTOR = "codex_in_app_browser";
export const CAPTURE_METHOD = "iab-expanded-viewport-screenshot";
export const CAPTURE_CONTRACT_ID = "iab-expanded-viewport-v1";

export class ScreenshotCaptureError extends Error {
  constructor(code, message, options = {}) {
    super(message, options);
    this.name = "ScreenshotCaptureError";
    this.code = code;
  }
}

function asBytes(raw) {
  if (raw instanceof Uint8Array) return raw;
  if (raw instanceof ArrayBuffer) return new Uint8Array(raw);
  if (ArrayBuffer.isView(raw)) {
    return new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength);
  }
  throw new ScreenshotCaptureError(
    "capture_failed",
    "tab.screenshot() must return an ArrayBuffer or Uint8Array",
  );
}

function isWithin(root, target) {
  const distance = relative(root, target);
  return distance === "" ||
    (!distance.startsWith(`..${sep}`) && distance !== ".." && !isAbsolute(distance));
}

function assertFileNameStem(fileNameStem) {
  if (
    typeof fileNameStem !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(fileNameStem) ||
    fileNameStem === "." ||
    fileNameStem === ".."
  ) {
    throw new ScreenshotCaptureError(
      "invalid_file_name",
      "fileNameStem must be a simple filename stem without path separators",
    );
  }
}

function assertAbsoluteDirectory(outputDirectory) {
  if (typeof outputDirectory !== "string" || !isAbsolute(outputDirectory)) {
    throw new ScreenshotCaptureError(
      "path_outside_workspace",
      "outputDirectory must be an absolute path inside a declared workspace root",
    );
  }
}

async function existingDirectoryAncestor(directory) {
  let candidate = resolve(directory);
  while (true) {
    try {
      const stats = await lstat(candidate);
      if (stats.isSymbolicLink()) {
        throw new ScreenshotCaptureError(
          "path_symlink_escape",
          `path contains a symbolic link: ${candidate}`,
        );
      }
      if (!stats.isDirectory()) {
        throw new ScreenshotCaptureError(
          "path_not_directory",
          `path component is not a directory: ${candidate}`,
        );
      }
      return candidate;
    } catch (error) {
      if (error instanceof ScreenshotCaptureError) throw error;
      if (error?.code !== "ENOENT") throw error;
      const parent = dirname(candidate);
      if (parent === candidate) {
        throw new ScreenshotCaptureError(
          "workspace_write_unavailable",
          `no existing ancestor for ${directory}`,
        );
      }
      candidate = parent;
    }
  }
}

async function validateTarget({
  outputDirectory,
  fileName,
  workspaceRoots,
  checkDestination = false,
}) {
  assertAbsoluteDirectory(outputDirectory);
  if (!Array.isArray(workspaceRoots) || workspaceRoots.length === 0) {
    throw new ScreenshotCaptureError(
      "workspace_write_unavailable",
      "workspaceRoots must contain at least one absolute workspace root",
    );
  }

  const outputPath = resolve(outputDirectory);
  const targetPath = join(outputPath, fileName);
  let selectedRoot = null;

  for (const rootValue of workspaceRoots) {
    if (typeof rootValue !== "string" || !isAbsolute(rootValue)) continue;
    const root = resolve(rootValue);
    try {
      const rootStats = await lstat(root);
      if (rootStats.isSymbolicLink() || !rootStats.isDirectory()) continue;
      if (isWithin(root, outputPath) && isWithin(root, targetPath)) {
        selectedRoot = root;
        break;
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }

  if (selectedRoot == null) {
    throw new ScreenshotCaptureError(
      "path_outside_workspace",
      `path is outside writable workspace roots: ${outputDirectory}`,
    );
  }

  const ancestor = await existingDirectoryAncestor(outputPath);
  const [rootReal, ancestorReal] = await Promise.all([
    realpath(selectedRoot),
    realpath(ancestor),
  ]);
  if (!isWithin(rootReal, ancestorReal)) {
    throw new ScreenshotCaptureError(
      "path_symlink_escape",
      `resolved path escapes workspace root: ${outputDirectory}`,
    );
  }

  if (checkDestination) {
    try {
      const targetStats = await lstat(targetPath);
      if (targetStats.isSymbolicLink()) {
        throw new ScreenshotCaptureError(
          "path_symlink_escape",
          `destination is a symbolic link: ${targetPath}`,
        );
      }
      throw new ScreenshotCaptureError(
        "destination_exists",
        `refusing to overwrite existing destination: ${targetPath}`,
      );
    } catch (error) {
      if (error instanceof ScreenshotCaptureError) throw error;
      if (error?.code !== "ENOENT") throw error;
    }
  }

  return { root: selectedRoot, outputDirectory: outputPath, targetPath };
}

function readPngDimensions(bytes) {
  const signature = [137, 80, 78, 71, 13, 10, 26, 10];
  if (bytes.length < 24 || !signature.every((value, index) => bytes[index] === value)) return null;
  return {
    format: "png",
    extension: "png",
    mimeType: "image/png",
    width: new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(16),
    height: new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(20),
  };
}

function readJpegDimensions(bytes) {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
  let offset = 2;
  while (offset + 3 < bytes.length) {
    if (bytes[offset] !== 0xff) {
      offset += 1;
      continue;
    }
    while (offset < bytes.length && bytes[offset] === 0xff) offset += 1;
    if (offset >= bytes.length) break;
    const marker = bytes[offset++];
    if (marker === 0xd8 || marker === 0xd9 || (marker >= 0xd0 && marker <= 0xd7) || marker === 0x01) {
      continue;
    }
    if (offset + 1 >= bytes.length) break;
    const segmentLength = (bytes[offset] << 8) | bytes[offset + 1];
    if (segmentLength < 2 || offset + segmentLength > bytes.length) break;
    const isStartOfFrame =
      (marker >= 0xc0 && marker <= 0xc3) ||
      (marker >= 0xc5 && marker <= 0xc7) ||
      (marker >= 0xc9 && marker <= 0xcb) ||
      (marker >= 0xcd && marker <= 0xcf);
    if (isStartOfFrame && segmentLength >= 7) {
      return {
        format: "jpeg",
        extension: "jpg",
        mimeType: "image/jpeg",
        height: (bytes[offset + 3] << 8) | bytes[offset + 4],
        width: (bytes[offset + 5] << 8) | bytes[offset + 6],
      };
    }
    offset += segmentLength;
  }
  return null;
}

function inspectImage(raw) {
  const bytes = asBytes(raw);
  const image = readPngDimensions(bytes) ?? readJpegDimensions(bytes);
  if (image == null || image.width <= 0 || image.height <= 0) {
    throw new ScreenshotCaptureError(
      "unsupported_image_format",
      "screenshot bytes are not a supported PNG or JPEG image",
    );
  }
  return { bytes, ...image };
}

function digest(bytes) {
  return createHash("sha256").update(Buffer.from(bytes)).digest("hex");
}

async function writeNoReplace({ bytes, outputDirectory, targetPath }) {
  await mkdir(outputDirectory, { recursive: true });
  const temporaryPath = join(
    outputDirectory,
    `.codex-screenshot-${randomUUID()}.tmp`,
  );
  let handle;
  try {
    handle = await open(temporaryPath, "wx", 0o600);
    await handle.write(Buffer.from(bytes));
    await handle.sync();
    await handle.close();
    handle = undefined;
    await link(temporaryPath, targetPath);
    await unlink(temporaryPath);
  } catch (error) {
    if (handle !== undefined) await handle.close().catch(() => {});
    await unlink(temporaryPath).catch(() => {});
    if (error?.code === "EEXIST") {
      throw new ScreenshotCaptureError(
        "destination_exists",
        `refusing to overwrite existing destination: ${targetPath}`,
        { cause: error },
      );
    }
    throw new ScreenshotCaptureError(
      "write_failed",
      `could not save screenshot: ${targetPath}`,
      { cause: error },
    );
  }
}

export async function preflightWorkspace({
  outputDirectory,
  workspaceRoots,
  fileNameStem = "capture",
  extension = "jpg",
} = {}) {
  try {
    assertFileNameStem(fileNameStem);
    const normalizedExtension = String(extension).replace(/^\./, "").toLowerCase();
    if (!/^[a-z0-9]{1,8}$/.test(normalizedExtension)) {
      throw new ScreenshotCaptureError("invalid_file_name", "invalid file extension");
    }
    const target = await validateTarget({
      outputDirectory,
      fileName: `${fileNameStem}.${normalizedExtension}`,
      workspaceRoots,
    });
    return { status: "available", ...target };
  } catch (error) {
    const normalized = error instanceof ScreenshotCaptureError
      ? error
      : new ScreenshotCaptureError("workspace_write_unavailable", String(error), { cause: error });
    return {
      status: "unavailable",
      errorCode: normalized.code,
      error: normalized.message,
    };
  }
}

export async function saveBrowserScreenshot({
  raw,
  outputDirectory,
  workspaceRoots,
  fileNameStem = "screenshot",
  capturedUrl = null,
  viewport = null,
  viewportOverride = null,
  deviceScaleFactor = null,
  cropBox = null,
  contentBounds = null,
  evidenceText = null,
} = {}) {
  assertFileNameStem(fileNameStem);
  const image = inspectImage(raw);
  const target = await validateTarget({
    outputDirectory,
    fileName: `${fileNameStem}.${image.extension}`,
    workspaceRoots,
    checkDestination: true,
  });
  await writeNoReplace({ bytes: image.bytes, ...target });
  return {
    path: target.targetPath,
    bytes: image.bytes.byteLength,
    sha256: digest(image.bytes),
    mimeType: image.mimeType,
    format: image.format,
    originalDimensions: { width: image.width, height: image.height },
    capturedUrl,
    capturedAt: new Date().toISOString(),
    viewport,
    viewportOverride,
    deviceScaleFactor,
    cropBox,
    contentBounds,
    evidenceText,
  };
}

export async function writeCaptureReceipt({
  outputDirectory,
  workspaceRoots,
  receipt,
} = {}) {
  const bytes = new TextEncoder().encode(`${JSON.stringify(receipt, null, 2)}\n`);
  const target = await validateTarget({
    outputDirectory,
    fileName: "capture.json",
    workspaceRoots,
    checkDestination: true,
  });
  await writeNoReplace({ bytes, ...target });
  return target.targetPath;
}
