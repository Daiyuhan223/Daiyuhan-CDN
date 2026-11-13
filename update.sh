#!/bin/bash
# CDN Manager 无损升级脚本
# 用法（在服务器上拿到新版代码后）：
#   cd /opt/cdn-update && bash update.sh
# 或指定安装目录： CDN_INSTALL_DIR=/opt/cdn-manager bash update.sh
#
# 特性：
#   - 保留 data/（SQLite 数据库、面板密码等），数据库结构自动迁移
#   - 升级前自动备份当前代码到 /opt/cdn-manager-backup/<时间戳>（不含 data），可回滚
#   - 语法自检通过后才重启服务；失败自动还原
#   - 升级后自动清空节点 hash，60s 内边缘节点全量重同步新配置
#   - 边缘节点 agent 会在一小时内通过 /api/v1/agent/download 自动升级（含自愈/磁盘清理）

set -e

INSTALL_DIR="${CDN_INSTALL_DIR:-/opt/cdn-manager}"
BACKUP_ROOT="/opt/cdn-manager-backup"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo " CDN Manager 升级"
echo "  安装目录 : $INSTALL_DIR"
echo "  升级来源 : $SRC"
echo "============================================"

[ "$(id -u)" != "0" ] && echo "ERROR: must be root" && exit 1
[ ! -f "$INSTALL_DIR/run_manager.py" ] && echo "ERROR: 未检测到已安装的 CDN Manager ($INSTALL_DIR)。请先 setup.sh 安装。" && exit 1
[ ! -d "$SRC/manager" ] && echo "ERROR: $SRC 不是完整的代码目录（缺少 manager/）。" && exit 1

SYNC_CMD=""
if command -v rsync >/dev/null 2>&1; then
    SYNC_CMD="rsync"
elif command -v tar >/dev/null 2>&1; then
    SYNC_CMD="tar"
else
    echo "ERROR: 需要 rsync 或 tar"
    exit 1
fi

restore_backup() {
    local b="$1"
    echo "  还原备份: $b"
    if [ "$SYNC_CMD" = "rsync" ]; then
        rsync -a --delete --exclude 'data/' "$b/" "$INSTALL_DIR/"
    else
        (cd "$b" && tar --exclude='./data' -cf - .) | (cd "$INSTALL_DIR" && tar -xf -)
    fi
}

echo ""
echo "[1/5] 备份当前代码..."
TS=$(date +%Y%m%d_%H%M%S)
BK="$BACKUP_ROOT/$TS"
mkdir -p "$BK"
if [ "$SYNC_CMD" = "rsync" ]; then
    rsync -a --exclude 'data/' --exclude '.git/' "$INSTALL_DIR/" "$BK/"
else
    (cd "$INSTALL_DIR" && tar --exclude='./data' --exclude='./.git' -cf - .) | (cd "$BK" && tar -xf -)
fi
# 清理旧备份，保留最近 3 份
ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | head -n -3 | xargs -r rm -rf
echo "  备份完成: $BK"

echo ""
echo "[2/5] 停止服务并更新代码..."
systemctl stop cdn-manager 2>/dev/null || true

SRC_REAL="$(realpath "$SRC" 2>/dev/null || echo "$SRC")"
INSTALL_REAL="$(realpath "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"
if [ "$SRC_REAL" = "$INSTALL_REAL" ]; then
    echo "  源码目录 == 安装目录，跳过文件复制（原地更新，仅重编译并重启）"
elif [ "$SYNC_CMD" = "rsync" ]; then
    rsync -a --delete --exclude 'data/' --exclude '.git/' --exclude '__pycache__/' \
        --exclude '*.pyc' --exclude "$BACKUP_ROOT" "$SRC/" "$INSTALL_DIR/"
else
    (cd "$SRC" && tar --exclude='./data' --exclude='./.git' \
        --exclude='./__pycache__' --exclude='*.pyc' -cf - .) | (cd "$INSTALL_DIR" && tar -xf -)
    echo "  (提示: tar 模式不删除旧文件；如需彻底清理请安装 rsync)"
fi
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

echo ""
echo "[3/5] Python 语法自检..."
cd "$INSTALL_DIR"
PYFILES=$(find . -maxdepth 3 -name '*.py' -not -path './data/*' | tr '\n' ' ')
if ! python3 -m py_compile $PYFILES 2>/tmp/cdn_pyerr; then
    echo "ERROR: 语法自检失败，回滚到上一个可用版本..."
    systemctl stop cdn-manager 2>/dev/null || true
    restore_backup "$BK"
    systemctl start cdn-manager 2>/dev/null || true
    cat /tmp/cdn_pyerr
    echo "已回滚。请检查代码后重试。"
    exit 1
fi
rm -rf "$INSTALL_DIR"/*/__pycache__ 2>/dev/null || true
echo "  语法自检通过"

echo ""
echo "[4/5] 刷新 systemd 并启动..."
cp "$INSTALL_DIR/manager/cdn-manager.service" /etc/systemd/system/cdn-manager.service

# 确保 Python 依赖（优先 apt，避免 PEP668 / 系统 Python 污染）
if ! python3 -c "import flask, jinja2, requests" >/dev/null 2>&1; then
    echo "  安装缺失的 Python 依赖..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-flask python3-jinja2 python3-requests 2>/dev/null \
        || pip3 install -q flask jinja2 requests --break-system-packages 2>/dev/null \
        || pip3 install -q flask jinja2 requests 2>/dev/null \
        || echo "  WARN: Python 依赖安装失败，请手动检查"
fi

systemctl daemon-reload
systemctl enable cdn-manager >/dev/null 2>&1 || true
systemctl restart cdn-manager

echo ""
echo "[5/5] 健康检查 + 触发节点重同步..."
OK=0
for i in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
        OK=1; break
    fi
    sleep 1
done
if [ "$OK" != "1" ]; then
    echo "ERROR: 服务未通过健康检查。查看日志: journalctl -u cdn-manager -n 50"
    echo "最简单回滚: systemctl stop cdn-manager; rm -rf \"$INSTALL_DIR\"/{manager,templates,agent,run_manager.py,*.sh,requirements.txt}; cp -r \"$BK/\"* \"$INSTALL_DIR/\"; systemctl start cdn-manager"
    exit 1
fi

# 清空节点 hash，强制全部节点下一次轮询即拉取全量新配置
python3 - <<'PY'
from manager.database import Database
db = Database()
for n in db.list_nodes():
    db.update_node_config_hash(n.id, None)
print(f"  已清空 {len(db.list_nodes())} 个节点的配置 hash")
PY

echo ""
echo "============================================"
echo " 升级完成!"
echo "  备份目录 : $BK"
echo "  回滚方法 : systemctl stop cdn-manager; rm -rf \"$INSTALL_DIR\"/{manager,templates,agent,run_manager.py,requirements.txt}; cp -r \"$BK/\"* \"$INSTALL_DIR/\"; systemctl start cdn-manager"
echo ""
echo "  边缘节点将在 60s 内自动重同步新配置；"
echo "  新版 agent（nginx 自愈 / 磁盘自动清理）将通过自更新在 1 小时内自动下发，"
echo "  如需立即生效，在每个节点执行: systemctl restart cdn-agent"
echo "============================================"
