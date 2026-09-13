#!/bin/bash
set -e

INSTALL_DIR="/opt/cdn-manager"
MARKER="${INSTALL_DIR}/.setup_done"
SELF="$(cd "$(dirname "$0")" && pwd)/setup.sh"

if [ -f "$MARKER" ]; then
    [ -f "$SELF" ] && rm -f "$SELF"
    exit 0
fi

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1

echo "============================================"
echo " CDN Manager Setup"
echo "============================================"

# OS
[ -f /etc/os-release ] && . /etc/os-release && echo "Detected: $NAME $VERSION_ID"

# Packages
echo "[1/4] Installing packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3-full curl unzip

# Dirs & copy
echo "[2/4] Copying files..."
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/data"
SRC="$(cd "$(dirname "$0")" && pwd)"
[ "$SRC" != "$INSTALL_DIR" ] && cp -r "$SRC"/* "$INSTALL_DIR/"

# Python deps
echo "[3/4] Installing Python deps..."
cd "$INSTALL_DIR"
pip3 install -q flask jinja2 requests 2>/dev/null || pip3 install -q flask jinja2 requests --break-system-packages

# Systemd
echo "[4/4] Installing service..."
cp "$INSTALL_DIR/manager/cdn-manager.service" /etc/systemd/system/
chmod +x "$INSTALL_DIR/dyh-cdn"
ln -sf "$INSTALL_DIR/dyh-cdn" /usr/local/bin/dyh-cdn
systemctl daemon-reload
systemctl enable cdn-manager
systemctl restart cdn-manager

sleep 2
if systemctl is-active --quiet cdn-manager; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "Done!"
    echo "  API:    http://${IP}:8080/"
    echo "  GUI:    http://${IP}:8080/cdn-gui/"
    echo "  Health: http://${IP}:8080/health"
    echo "  CLI:    dyh-cdn"
    echo ""
    date > "$MARKER"
    [ -f "$SELF" ] && rm -f "$SELF"
else
    echo "ERROR: service failed. journalctl -u cdn-manager -n 30"
    exit 1
fi
