#!/usr/bin/env bash
# ============================================================
# prepare_env.sh
# 一次性准备构建所需的静态资源，保存为仓库内的 tarball。
#
# 使用方法:
#   1. chmod +x prepare_env.sh
#   2. ./prepare_env.sh
#   3. git add scripts/repo scripts/toolchain.tar.gz scripts/mkbootimg.tar.gz
#   4. git commit -m "chore: add pre-built build environment"
#
# 之后 workflow 会自动从这些本地文件解压，不再需要联网下载。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> 下载 repo 工具..."
curl -fsSL "https://storage.googleapis.com/git-repo-downloads/repo" -o "$SCRIPT_DIR/repo"
chmod +x "$SCRIPT_DIR/repo"
echo "  ✅ repo 工具已保存到: $SCRIPT_DIR/repo"

echo ""
echo "==> 克隆 build-tools (toolchain)..."
git clone \
  https://android.googlesource.com/kernel/prebuilts/build-tools \
  -b main-kernel-build-2024 \
  --depth 1 \
  "$WORK_DIR/toolchain"
echo "  ✅ build-tools 克隆完成"

echo ""
echo "==> 克隆 mkbootimg..."
git clone \
  https://android.googlesource.com/platform/system/tools/mkbootimg \
  -b main-kernel-build-2024 \
  --depth 1 \
  "$WORK_DIR/mkbootimg"
echo "  ✅ mkbootimg 克隆完成"

echo ""
echo "==> 打包 toolchain.tar.gz..."
cd "$WORK_DIR"
tar czf "$SCRIPT_DIR/toolchain.tar.gz" toolchain
echo "  ✅ toolchain.tar.gz 已保存 ($(du -h "$SCRIPT_DIR/toolchain.tar.gz" | cut -f1))"

echo "==> 打包 mkbootimg.tar.gz..."
tar czf "$SCRIPT_DIR/mkbootimg.tar.gz" mkbootimg
echo "  ✅ mkbootimg.tar.gz 已保存 ($(du -h "$SCRIPT_DIR/mkbootimg.tar.gz" | cut -f1))"

echo ""
echo "============================================================"
echo "准备完成！现在执行:"
echo "  git add scripts/repo scripts/toolchain.tar.gz scripts/mkbootimg.tar.gz"
echo "  git commit -m 'chore: add pre-built build environment'"
echo "============================================================"