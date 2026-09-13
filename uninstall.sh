#!/bin/bash
# CDN 系统卸载脚本

echo "============================================"
echo " CDN 系统卸载"
echo "============================================"
echo ""

if [ "$(id -u)" != "0" ]; then
    echo "ERROR: must be root"
    exit 1
fi

read -p "  确定卸载 CDN 系统？Manager 也会被停掉！[y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "  取消。"
    exit 0
fi

echo ""
echo "[1/3] 停止服务..."
systemctl stop cdn-manager 2>/dev/null || true
systemctl stop cdn-agent 2>/dev/null || true
systemctl disable cdn-manager 2>/dev/null || true
systemctl disable cdn-agent 2>/dev/null || true

echo "[2/3] 删除文件..."
rm -f /usr/local/bin/dyh-cdn
rm -f /etc/systemd/system/cdn-manager.service
rm -f /etc/systemd/system/cdn-agent.service
systemctl daemon-reload
rm -rf /opt/cdn-manager
rm -rf /opt/cdn-agent
rm -rf /var/cache/nginx/cdn_cache

echo "[3/3] 清理完成。"
echo ""
echo "  提醒：Nginx 不会卸载（系统自带），如需卸载："
echo "    apt-get remove -y nginx"
echo "    rm -f /etc/apt/sources.list.d/nginx.list"
