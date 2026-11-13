#!/bin/bash
# Daiyuhan-CDN 一键安装 (Manager)
#   curl -fsSL https://raw.githubusercontent.com/Daiyuhan223/Daiyuhan-CDN/main/dyh-cdn.sh | bash
# 可选环境变量：
#   CDN_REF         分支或 tag（默认 main）
#   CDN_REPO        GitHub 仓库（默认 Daiyuhan223/Daiyuhan-CDN）
#   CDN_MIRROR_ZIP  镜像兜底 zip（GitHub 不可达时使用）

set -e

INSTALL_DIR="/opt/cdn-manager"
REPO="${CDN_REPO:-Daiyuhan223/Daiyuhan-CDN}"
REF="${CDN_REF:-main}"
MIRROR_ZIP="${CDN_MIRROR_ZIP:-https://cdn.huaiyi.ltd/d/dyh-cdn.zip}"

echo "============================================"
echo " Daiyuhan-CDN 一键安装"
echo "   仓库: $REPO"
echo "   版本: $REF"
echo "============================================"
echo ""

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1

echo "[1/3] 安装依赖..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl tar unzip ca-certificates python3 python3-pip python3-venv python3-full

TMP="$(mktemp -d /tmp/cdn-install.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "[2/3] 下载最新代码..."
OK=0
SRC=""
for url in \
    "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" \
    "https://github.com/$REPO/archive/refs/tags/$REF.tar.gz" \
    "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" \
    "https://codeload.github.com/$REPO/tar.gz/refs/tags/$REF"; do
    if curl -fsSL --connect-timeout 15 --retry 3 "$url" -o "$TMP/pkg.tar.gz" 2>/dev/null \
       && tar -xzf "$TMP/pkg.tar.gz" -C "$TMP" 2>/dev/null; then
        SRC="$(find "$TMP" -maxdepth 2 -name setup.sh -exec dirname {} \; 2>/dev/null | head -1)"
        if [ -n "$SRC" ]; then OK=1; break; fi
    fi
done

if [ "$OK" != "1" ]; then
    echo "  GitHub 拉取失败，尝试镜像 $MIRROR_ZIP ..."
    command -v unzip >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unzip >/dev/null 2>&1 || true
    if curl -fsSL --connect-timeout 15 --retry 3 "$MIRROR_ZIP" -o "$TMP/pkg.zip" 2>/dev/null \
       && unzip -q "$TMP/pkg.zip" -d "$TMP/mirror" 2>/dev/null; then
        SRC="$(find "$TMP/mirror" -maxdepth 2 -name setup.sh -exec dirname {} \; 2>/dev/null | head -1)"
        [ -n "$SRC" ] && OK=1
    fi
fi
[ "$OK" != "1" ] && echo "ERROR: 无法获取代码（GitHub 与镜像均失败）" && exit 1

echo "  代码目录: $SRC"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -a "$SRC"/. "$INSTALL_DIR"/

echo "[3/3] 运行安装..."
cd "$INSTALL_DIR"
chmod +x setup.sh
./setup.sh
