#!/bin/bash
# CDN Edge Node Installer — auto-detects Baota vs blank Debian.
set -e

MANAGER_URL="${MANAGER_URL:-}"
REGISTER_TOKEN="${REGISTER_TOKEN:-}"
AGENT_DIR="/opt/cdn-agent"

if [ -z "$MANAGER_URL" ] || [ -z "$REGISTER_TOKEN" ]; then
    echo "ERROR: MANAGER_URL or REGISTER_TOKEN not set"
    exit 1
fi

echo "============================================"
echo " CDN Edge Node Installer"
echo " Manager: $MANAGER_URL"
echo "============================================"

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1

if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "Detected OS: $NAME $VERSION_ID"
fi

# ── Environment detection ─────────────────────────────────────────
# Real detection: Baota nginx binary/config present (not just the panel dir).
IS_BAOTA=0
if [ -x /www/server/nginx/sbin/nginx ] || [ -f /www/server/nginx/conf/nginx.conf ]; then
    IS_BAOTA=1
fi

if [ "$IS_BAOTA" = "1" ]; then
    echo "Detected environment: Baota (using Baota Nginx, will NOT install system Nginx)"
else
    echo "Detected environment: blank Debian (will install nginx.org Nginx)"
fi

echo ""
echo "[1/5] Installing packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates

# Agent is stdlib-only (urllib); no pip / no requests required.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3

echo "[2/5] Setting up Nginx..."

if [ "$IS_BAOTA" = "1" ]; then
    # Remove any leftover system Nginx (from a previously broken install) so it
    # can't fight Baota over :80/:443 after a reboot.
    systemctl stop nginx 2>/dev/null || true
    systemctl disable nginx 2>/dev/null || true
    if dpkg -l 2>/dev/null | grep -qE '^ii[[:space:]]+(nginx|nginx-common|nginx-full|nginx-extras|nginx-core)([[:space:]]|$)'; then
        echo "  Purging leftover system Nginx packages (Baota manages Nginx)..."
        DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq nginx nginx-common nginx-full nginx-extras nginx-core 2>/dev/null || true
    fi
    # If a systemd nginx unit somehow remains enabled, mask it so it never autostarts.
    if systemctl list-unit-files 2>/dev/null | grep -q '^nginx\.service' \
       && systemctl is-enabled nginx >/dev/null 2>&1; then
        systemctl mask nginx 2>/dev/null || true
    fi
    if [ ! -x /www/server/nginx/sbin/nginx ]; then
        echo "ERROR: Baota detected but /www/server/nginx/sbin/nginx not found."
        echo "       Please install Nginx from the Baota panel first."
        exit 1
    fi
else
    # Blank Debian: install nginx.org official Nginx.
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gnupg lsb-release
    curl --connect-timeout 10 --retry 2 -fsSL https://nginx.org/keys/nginx_signing.key | gpg --dearmor \
        | tee /usr/share/keyrings/nginx-archive-keyring.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] \
http://nginx.org/packages/debian $(lsb_release -cs) nginx" \
        | tee /etc/apt/sources.list.d/nginx.list >/dev/null
    apt-get update -qq

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
    systemctl enable nginx
fi

echo "[3/5] Deploying agent..."
mkdir -p "$AGENT_DIR" /opt/cdn-agent/backup

if ! curl -fsS --connect-timeout 5 "${MANAGER_URL}/health" >/dev/null 2>&1; then
    echo "ERROR: Manager unreachable at ${MANAGER_URL}"
    exit 1
fi

TMP_AGENT="$AGENT_DIR/cdn_agent.py.tmp"
if ! curl -fsSL --connect-timeout 10 --retry 3 "${MANAGER_URL}/api/v1/agent/download" -o "$TMP_AGENT"; then
    echo "ERROR: failed to download agent from ${MANAGER_URL}"
    exit 1
fi
# Validate before installing: must look like our agent and be valid Python.
if ! head -n1 "$TMP_AGENT" | grep -q '^#!' \
   || ! grep -q 'def run(' "$TMP_AGENT" \
   || ! python3 -c "compile(open('$TMP_AGENT', encoding='utf-8').read(), 'cdn_agent', 'exec')" 2>/dev/null; then
    echo "ERROR: downloaded agent is invalid (manager returned an error page?)"
    rm -f "$TMP_AGENT"
    exit 1
fi
mv "$TMP_AGENT" "$AGENT_DIR/cdn_agent.py"
chmod +x "$AGENT_DIR/cdn_agent.py"

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

systemctl daemon-reload
systemctl enable cdn-agent

echo "[4/5] Registering with manager..."
python3 "$AGENT_DIR/cdn_agent.py" --register --manager "$MANAGER_URL" --token "$REGISTER_TOKEN"

echo "[5/5] Starting agent..."
systemctl restart cdn-agent

# ── Post-install validation ───────────────────────────────────────
echo ""
echo "Validating..."
sleep 3

NGX_BIN="/usr/sbin/nginx"
NGX_CONF="/etc/nginx/nginx.conf"
SITE_DIR="/etc/nginx/conf.d/cdn"
if [ "$IS_BAOTA" = "1" ]; then
    NGX_BIN="/www/server/nginx/sbin/nginx"
    NGX_CONF="/www/server/nginx/conf/nginx.conf"
    SITE_DIR="/www/server/panel/vhost/nginx"
fi

FAIL=0

if [ -x "$NGX_BIN" ] && "$NGX_BIN" -t -c "$NGX_CONF" >/dev/null 2>&1; then
    echo "  [ok] nginx config valid ($NGX_BIN)"
else
    echo "  [!!] nginx config test failed: $NGX_BIN -t -c $NGX_CONF"
    FAIL=1
fi

if pgrep -f "$NGX_BIN" >/dev/null 2>&1 || systemctl is-active --quiet nginx 2>/dev/null; then
    echo "  [ok] nginx process running"
else
    echo "  [!!] nginx is not running"
    FAIL=1
fi

# 只有配置了 SSL 站点时才要求 443 监听（新节点尚未下发 SSL 站点属正常）
if grep -rqs "ssl_certificate" "$SITE_DIR"/*.conf 2>/dev/null; then
    if command -v ss >/dev/null 2>&1; then
        LISTEN="$(ss -ltn 2>/dev/null)"
    else
        LISTEN="$(cat /proc/net/tcp /proc/net/tcp6 2>/dev/null)"
    fi
    if echo "$LISTEN" | grep -qE ':443|:01BB'; then
        echo "  [ok] port 443 listening"
    else
        echo "  [!!] port 443 is NOT listening (HTTPS sites will fail)"
        FAIL=1
    fi
else
    echo "  [--] no SSL site yet, skip 443 check"
fi

if systemctl is-active --quiet cdn-agent; then
    echo "  [ok] cdn-agent service active"
else
    echo "  [!!] cdn-agent service not active"
    FAIL=1
fi

echo ""
if [ "$FAIL" = "0" ]; then
    echo "Done. Node healthy."
else
    echo "安装完成，但存在告警。请检查："
    echo "  - nginx:  $NGX_BIN -t -c $NGX_CONF ; journalctl -u cdn-agent -n 50"
    echo "  - agent:  python3 $AGENT_DIR/cdn_agent.py --diag"
fi
echo "  systemctl status cdn-agent"
