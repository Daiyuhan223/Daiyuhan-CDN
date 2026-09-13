#!/bin/bash
set -e

MANAGER_URL="${MANAGER_URL:-}"
REGISTER_TOKEN="${REGISTER_TOKEN:-}"

if [ -z "$MANAGER_URL" ] || [ -z "$REGISTER_TOKEN" ]; then
    echo "ERROR: MANAGER_URL or REGISTER_TOKEN not set"
    exit 1
fi

echo "============================================"
echo " CDN Edge Node Installer"
echo " Manager: $MANAGER_URL"
echo "============================================"

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "Detected: $NAME $VERSION_ID"
else
    echo "WARNING: Cannot detect OS, proceeding..."
fi

echo ""
echo "[1/5] Installing Nginx (nginx.org official)..."
apt-get update -qq
apt-get install -y -qq curl gnupg lsb-release

curl --connect-timeout 10 --retry 2 -fsSL https://nginx.org/keys/nginx_signing.key | gpg --dearmor \
    | tee /usr/share/keyrings/nginx-archive-keyring.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] \
http://nginx.org/packages/debian $(lsb_release -cs) nginx" \
    | tee /etc/apt/sources.list.d/nginx.list >/dev/null
apt-get update -qq

# Kill any existing nginx processes, remove old init scripts
systemctl stop nginx 2>/dev/null || true
/etc/init.d/nginx stop 2>/dev/null || true
pkill -9 nginx 2>/dev/null || true
sleep 1

if dpkg -l nginx-extras &>/dev/null 2>&1; then
    apt-get remove -y -qq nginx-extras nginx-common
fi
rm -f /etc/init.d/nginx /etc/systemd/system/nginx.service 2>/dev/null

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::="--force-confold" nginx
systemctl daemon-reload

echo "[2/5] Installing Python..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-pip python3-full python3-venv
pip3 install -q requests 2>/dev/null || pip3 install -q requests --break-system-packages 2>/dev/null || pip3 install -q requests

echo "[3/5] Deploying agent..."
mkdir -p /opt/cdn-agent

if curl -s --connect-timeout 5 "${MANAGER_URL}/health" >/dev/null 2>&1; then
    curl --connect-timeout 10 --retry 2 -sSL "${MANAGER_URL}/api/v1/agent/download" -o /opt/cdn-agent/cdn_agent.py
else
    echo "ERROR: Manager unreachable at ${MANAGER_URL}"
    exit 1
fi

cat > /etc/systemd/system/cdn-agent.service << 'EOF'
[Unit]
Description=CDN Edge Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/cdn-agent/cdn_agent.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /etc/nginx/conf.d/cdn /etc/nginx/ssl /var/cache/nginx/cdn_cache
systemctl daemon-reload
systemctl enable nginx

echo "[4/5] Registering with manager..."
python3 /opt/cdn-agent/cdn_agent.py --register --manager "$MANAGER_URL" --token "$REGISTER_TOKEN"

echo "[5/5] Starting agent..."
systemctl enable cdn-agent
systemctl start cdn-agent

echo ""
echo "Done. systemctl status cdn-agent"
