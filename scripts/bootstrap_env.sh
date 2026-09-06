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
else
  # Venv exists but may be corrupted after a cloud-computer reset that
  # partially wiped .local.  Verify it can actually run Python; if not,
  # rebuild it transparently.
  if ! "$PY" -c "import sys; sys.exit(0)" 2>/dev/null; then
    warn "虚拟环境已损坏，正在重建..."
    rm -rf "$PROJECT_DIR/OpenMontage/.venv"
    command -v python3 >/dev/null 2>&1 || die "找不到 python3"
    (cd "$PROJECT_DIR/OpenMontage" && python3 -m venv .venv) || die "venv 重建失败"
  fi
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

# --- 6. 同步入口 skill 到豆包 .user_skills（幂等）-------------------------
# 云电脑重置后 .user_skills 可能一并丢失，豆包就发现不了本 skill。
# 这里每次自检都重新同步一次，让定时任务具备自愈能力。
USER_SKILLS="${SUPER_DOUBAO_USER_SKILLS:-$HOME/.super_doubao/super-doubao-runtime/workspace/.user_skills}"
if [ -d "$USER_SKILLS" ]; then
  mkdir -p "$USER_SKILLS/ai-daily-news-studio"
  if cp -R "$PROJECT_DIR/entry-skill/ai-daily-news-studio/." "$USER_SKILLS/ai-daily-news-studio/" 2>/dev/null; then
    info "入口 skill 已同步到 $USER_SKILLS/ai-daily-news-studio"
  else
    warn "入口 skill 同步失败（不影响本次运行）"
  fi
else
  warn "未找到 .user_skills 目录，跳过 skill 同步：$USER_SKILLS"
fi

# --- 6b. 语音克隆参考音频检查 ----------------------------------------------
# example-audio.mp3 随仓库分发，是语音克隆的音色参考。云电脑重置后
# git pull 即可恢复。如果缺失则警告（不阻断，因为 text-to-audio 模式
# 仍可作为备选）。
REF_AUDIO="$PROJECT_DIR/example-audio.mp3"
if [ -f "$REF_AUDIO" ]; then
  REF_SIZE=$(stat -c%s "$REF_AUDIO" 2>/dev/null || stat -f%z "$REF_AUDIO" 2>/dev/null || echo 0)
  info "参考音频: example-audio.mp3 ($((REF_SIZE / 1024)) KB)"
else
  warn "参考音频 example-audio.mp3 缺失，语音克隆将不可用（git pull 可恢复）"
fi

# --- 6c. 磁盘空间检查 -------------------------------------------------------
# 视频渲染需要约 500MB-1GB 临时空间。低于 500MB 时警告。
if command -v df >/dev/null 2>&1; then
  FREE_KB=$(df -P "$PROJECT_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "${FREE_KB:-}" ] && [ "$FREE_KB" -lt 512000 ] 2>/dev/null; then
    warn "磁盘剩余空间不足: $((FREE_KB / 1024)) MB（视频渲染可能失败）"
  else
    info "磁盘空间: $((FREE_KB / 1024)) MB 可用"
  fi
fi

# --- 7. 终检 --------------------------------------------------------------
info "python : $("$PY" --version 2>&1)"
info "node   : $(node --version 2>/dev/null) / npm $(npm --version 2>/dev/null || echo N/A)"
info "ffmpeg : $(ffmpeg -version 2>/dev/null | head -n1 | awk '{print $3}')"
info "ffprobe: $(ffprobe -version 2>/dev/null | head -n1 | awk '{print $3}')"
echo "BOOTSTRAP_OK"
echo "后续命令请先执行： source $LOCAL_DIR/env.sh"
