#!/bin/bash
# Daiyuhan-CDN 在线一键升级
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Daiyuhan223/Daiyuhan-CDN/main/upgrade.sh | bash
#   bash upgrade.sh            # 默认拉取 main
#   bash upgrade.sh v0.1.0     # 拉取指定 tag/分支
#
# 说明：自动下载最新代码到临时目录后调用其中的 update.sh（备份/校验/回滚/重启一条龙），
#       保留 data/ 数据库与面板配置。可用环境变量覆盖：
#         CDN_REPO      GitHub 仓库 (默认 Daiyuhan223/Daiyuhan-CDN)
#         CDN_REF       分支或标签 (默认 main)
#         CDN_MIRROR_ZIP 镜像兜底 zip (默认 https://cdn.huaiyi.ltd/d/dyh-cdn.zip)

set -e

REPO="${CDN_REPO:-Daiyuhan223/Daiyuhan-CDN}"
REF="${1:-${CDN_REF:-main}}"
MIRROR_ZIP="${CDN_MIRROR_ZIP:-https://cdn.huaiyi.ltd/d/dyh-cdn.zip}"

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1

TMP="$(mktemp -d /tmp/cdn-upgrade.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "============================================"
echo " Daiyuhan-CDN 在线一键升级"
echo "   仓库 : $REPO"
echo "   版本 : $REF"
echo "============================================"
echo ""

echo "[1/3] 下载代码..."
OK=0
for url in \
    "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" \
    "https://github.com/$REPO/archive/refs/tags/$REF.tar.gz" \
    "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" \
    "https://codeload.github.com/$REPO/tar.gz/refs/tags/$REF"; do
    if curl -fsSL --connect-timeout 15 --retry 3 "$url" -o "$TMP/pkg.tar.gz" 2>/dev/null \
       && tar -xzf "$TMP/pkg.tar.gz" -C "$TMP" 2>/dev/null; then
        OK=1
        break
    fi
done

if [ "$OK" != "1" ]; then
    echo "  GitHub 拉取失败，尝试镜像 $MIRROR_ZIP ..."
    command -v unzip >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unzip >/dev/null 2>&1 || true
    if curl -fsSL --connect-timeout 15 --retry 3 "$MIRROR_ZIP" -o "$TMP/pkg.zip" 2>/dev/null \
       && command -v unzip >/dev/null 2>&1 \
       && unzip -q "$TMP/pkg.zip" -d "$TMP/mirror" 2>/dev/null; then
        OK=1
    fi
fi
[ "$OK" != "1" ] && echo "ERROR: 无法获取代码（GitHub 与镜像均失败）" && exit 1

SRC="$(find "$TMP" -maxdepth 3 -name update.sh -exec dirname {} \; 2>/dev/null | head -1)"
if [ -z "$SRC" ] || [ ! -f "$SRC/update.sh" ]; then
    echo "ERROR: 下载的代码中未找到 update.sh"
    exit 1
fi
echo "  代码目录: $SRC"

echo ""
echo "[2/3] 执行升级（保留 data/）..."
cd "$SRC"
chmod +x update.sh
bash update.sh

echo ""
echo "[3/3] 完成。"
