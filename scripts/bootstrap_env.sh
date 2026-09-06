#!/usr/bin/env bash
# AI 每日早报 · 环境自检与自动安装
#
# 设计前提：豆包云电脑运行一段时间后会被重置，因此本脚本必须幂等 ——
# 每次运行前都可以（也应该）重新执行一次。已具备的依赖会跳过，
# 缺失的会自动补装，全部装在项目内 .local/ 下，不依赖 root。
#
# 用法：bash scripts/bootstrap_env.sh
# 之后每条命令先执行：source .local/env.sh

set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCAL_DIR="$PROJECT_DIR/.local"
BIN_DIR="$LOCAL_DIR/bin"
NODE_DIR="$LOCAL_DIR/node"
PY="$PROJECT_DIR/OpenMontage/.venv/bin/python"

info() { echo "[env] $*"; }
warn() { echo "[env][warn] $*"; }
die()  { echo "[env][FATAL] $*"; exit 1; }

mkdir -p "$BIN_DIR"
export PATH="$BIN_DIR:$NODE_DIR/bin:$PATH"

# --- 1. Python 虚拟环境（渲染与流水线都在里面）---------------------------
if [ ! -x "$PY" ]; then
  info "创建 Python 虚拟环境（一次性，约 2-4 分钟）..."
  command -v python3 >/dev/null 2>&1 || die "找不到 python3"
  (cd "$PROJECT_DIR/OpenMontage" && python3 -m venv .venv) || die "venv 创建失败"
fi
info "安装/更新 Python 依赖..."
"$PY" -m pip install -q --upgrade pip 2>/dev/null || warn "pip 升级失败，继续"
"$PY" -m pip install -q -r "$PROJECT_DIR/OpenMontage/requirements.txt" || warn "OpenMontage 依赖安装异常"
if [ -f "$PROJECT_DIR/ai_morning_brief/requirements.txt" ]; then
  "$PY" -m pip install -q -r "$PROJECT_DIR/ai_morning_brief/requirements.txt" || warn "早报依赖安装异常"
fi

# --- 2. Node.js >= 22（HyperFrames 渲染经 npx 调用，必需）----------------
node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)"
  [ -n "$major" ] && [ "$major" -ge 22 ]
}
if ! node_ok; then
  info "安装 Node.js 22 LTS 到项目本地..."
  case "$(uname -m)" in
    x86_64)         NA="x64" ;;
    aarch64|arm64)  NA="arm64" ;;
    *) die "不支持的 CPU 架构: $(uname -m)" ;;
  esac

  NODE_VER="v22.20.0"
  # 优先探测当前 22.x 最新 LTS，探测失败则用上面的固定版本
  if [ -x "$PY" ]; then
    DETECTED="$("$PY" - <<'EOF' 2>/dev/null || true
import json, urllib.request
try:
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=15) as r:
        data = json.load(r)
    print(next(x["version"] for x in data if x.get("lts") and x["version"].startswith("v22.")))
except Exception:
    pass
EOF
)"
    [ -n "${DETECTED:-}" ] && NODE_VER="$DETECTED"
  fi

  info "使用 Node $NODE_VER"
  TARBALL="node-$NODE_VER-linux-$NA.tar.xz"
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/$TARBALL" -o "/tmp/$TARBALL" \
    || curl -fsSL "https://npmmirror.com/mirrors/node/$NODE_VER/$TARBALL" -o "/tmp/$TARBALL" \
    || die "Node.js 下载失败（官方源与 npmmirror 均失败）"
  mkdir -p "$NODE_DIR"
  tar -xJf "/tmp/$TARBALL" -C "$NODE_DIR" --strip-components=1 || die "Node.js 解压失败"
fi

# --- 3. ffmpeg + ffprobe（混音与质量门禁按 PATH 硬编码调用，必需）--------
have_ff() { command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; }
if ! have_ff; then
  info "安装 ffmpeg/ffprobe..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y ffmpeg >/dev/null 2>&1 \
      || sudo apt-get install -y ffmpeg >/dev/null 2>&1 \
      || warn "apt-get 不可用或无权限，改为下载静态构建"
  fi
  if ! have_ff; then
    FF_TAR="ffmpeg-master-latest-linux64-gpl.tar.xz"
    curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/$FF_TAR" -o "/tmp/$FF_TAR" \
      || curl -fsSL "https://ghfast.top/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/$FF_TAR" -o "/tmp/$FF_TAR" \
      || die "ffmpeg 下载失败"
    tar -xJf "/tmp/$FF_TAR" -C /tmp || die "ffmpeg 解压失败"
    FF_DIR="$(find /tmp -maxdepth 1 -type d -name 'ffmpeg-*-linux64*' | head -n1)"
    [ -n "$FF_DIR" ] || die "找不到解压后的 ffmpeg 目录"
    install -m 0755 "$FF_DIR/bin/ffmpeg" "$FF_DIR/bin/ffprobe" "$BIN_DIR/" || die "ffmpeg 安装失败"
  fi
fi
have_ff || die "ffmpeg/ffprobe 仍不可用，无法继续"

# --- 4. .env（仓库只带 .env.example）-------------------------------------
if [ ! -f "$PROJECT_DIR/.env" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env" && info "已从 .env.example 生成 .env"
fi

# --- 5. 生成 env.sh，供后续命令注入 PATH ---------------------------------
cat > "$LOCAL_DIR/env.sh" <<EOF
# 由 scripts/bootstrap_env.sh 生成，每次运行前 source 本文件
export PATH="$BIN_DIR:$NODE_DIR/bin:\$PATH"
export PROJECT_DIR="$PROJECT_DIR"
EOF

# --- 6. 终检 --------------------------------------------------------------
info "python : $("$PY" --version 2>&1)"
info "node   : $(node --version 2>/dev/null) / npm $(npm --version 2>/dev/null || echo N/A)"
info "ffmpeg : $(ffmpeg -version 2>/dev/null | head -n1 | awk '{print $3}')"
info "ffprobe: $(ffprobe -version 2>/dev/null | head -n1 | awk '{print $3}')"
echo "BOOTSTRAP_OK"
echo "后续命令请先执行： source $LOCAL_DIR/env.sh"
