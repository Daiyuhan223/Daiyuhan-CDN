#!/bin/bash
# CDN Manager 一键安装
# curl -fsSL https://cdn.huaiyi.ltd/d/dyh-cdn.sh | bash

set -e

INSTALL_DIR="/opt/cdn-manager"
ZIP_URL="https://cdn.huaiyi.ltd/d/dyh-cdn.zip"

echo "============================================"
echo " CDN Manager 一键安装"
echo "============================================"
echo ""

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1

# Install prerequisites
echo "[1/3] Installing prerequisites..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl unzip python3 python3-pip python3-venv python3-full

# Download and extract
echo "[2/3] Downloading..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
curl -fsSL --connect-timeout 30 --retry 3 "$ZIP_URL" -o /tmp/dyh-cdn.zip
unzip -qo /tmp/dyh-cdn.zip -d "$INSTALL_DIR"
rm -f /tmp/dyh-cdn.zip

# Run setup
echo "[3/3] Running setup..."
cd "$INSTALL_DIR"
chmod +x setup.sh
./setup.sh
