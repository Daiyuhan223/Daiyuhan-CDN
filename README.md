# Daiyuhan-CDN

[![Version](https://img.shields.io/badge/version-v0.1.1-blue.svg)](https://github.com/Daiyuhan223/Daiyuhan-CDN/releases)
[![Author](https://img.shields.io/badge/author-Daiyuhan-4361ee.svg)](https://github.com/Daiyuhan223)

基于 Nginx + Python 的 CDN（内容分发网络）。Manager 集中管控，边缘节点自动同步配置和 SSL 证书，支持通配符域名、HTTPS 回源、WebSocket、HTTP→HTTPS 跳转、301 重定向、证书库、GeoDNS 调度、Baota 面板节点。

- 项目主页：https://github.com/Daiyuhan223/Daiyuhan-CDN
- 作者：Daiyuhan（https://github.com/Daiyuhan223）
- 当前版本：v0.1.1

---

## 目录

- [架构](#架构)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [节点类型](#节点类型)
- [CLI 命令](#cli-命令)
- [Web 管理面板](#web-管理面板)
- [Agent 特性](#agent-特性)
- [缓存管理](#缓存管理)
- [SSL 证书管理](#ssl-证书管理)
- [WebSocket 支持](#websocket-支持)
- [自定义站点 Nginx 配置](#自定义站点-nginx-配置)
- [配置说明](#配置说明)
- [无损升级](#无损升级)
- [故障排查](#故障排查)

---

## 架构

```
                         ┌─────────────────────────┐
                         │     DNS / GeoDNS         │
                         │  cdn.<your_domain>       │
                         └───────────┬─────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
     ┌────────────────┐    ┌────────────────┐    ┌────────────────┐
     │  Edge Node     │    │  Edge Node     │    │  Baota Node    │
     │  Nginx :80/443 │    │  Nginx :80/443 │    │  Nginx :80/443 │
     │  proxy_cache   │    │  proxy_cache   │    │  proxy_cache   │
     │  cdn-agent     │    │  cdn-agent     │    │  cdn-agent     │
     │  /etc/nginx    │    │  /etc/nginx    │    │  /www/server   │
     └───────┬────────┘    └───────┬────────┘    └───────┬────────┘
             │                     │                     │
             │  cache miss         │                     │
             └─────────────────────┼─────────────────────┘
                                   ▼
                         ┌─────────────────┐
                         │  Origin Server   │
                         │  (用户源站)      │
                         └─────────────────┘

              ┌─────────────────────────────────────┐
              │         Manager (控制服务器)          │
              │  Flask API :8080  +  SQLite          │
              │  · 站点 & 节点管理                    │
              │  · Nginx 配置生成 (Jinja2)            │
              │  · SSL 证书签发 & 分发                │
              │  · Agent 同步 API                    │
              │  · Web 管理面板 (/cdn-gui/)          │
              └─────────────────────────────────────┘
```

**核心流程：**
1. Manager 集中管理所有站点和节点信息（SQLite）
2. 边缘节点的 `cdn-agent` 每 60 秒向 Manager 轮询配置更新
3. 检测到变更后自动拉取 Nginx 配置和 SSL 证书，`nginx -t` 通过后 reload
4. 用户请求到达边缘节点 → 缓存命中直接返回 → 缓存未命中回源抓取并缓存
5. DNS 层面通过 GeoDNS 将用户路由到最近的边缘节点

---

## 项目结构

```
Daiyuhan-CDN/
├── setup.sh                    # Manager 一键部署
├── dyh-cdn.sh                  # 在线一键安装脚本
├── update.sh                   # 无损升级脚本（保留 data/，需自备新代码）
├── upgrade.sh                  # 在线一键升级（自动拉取最新代码 + update.sh）
├── run_manager.py              # Manager 启动入口
├── requirements.txt            # Python 依赖
├── VERSION                     # 版本号（面板更新检查用）
├── manager/                    # 控制面
│   ├── app.py                  # Flask API + Web 面板路由
│   ├── cli.py                  # 命令行工具
│   ├── config.py               # 全局配置（唯一配置源）
│   ├── database.py             # SQLite 数据库（含自动迁移）
│   ├── models.py               # 数据模型
│   ├── nginx_gen.py            # Nginx 配置生成（Jinja2）
│   ├── ssl_manager.py          # SSL 证书签发/导入/域名校验
│   ├── auth.py                 # API Key 认证
│   ├── cdn-manager.service     # systemd 服务文件
│   └── templates/
│       ├── dashboard.html      # Web 管理面板（纯静态 HTML）
│       └── login.html          # 登录页
├── agent/                      # 边缘节点
│   ├── cdn_agent.py            # Agent（轮询同步 + 备份回滚 + 自更新 + 自愈 + 空间清理）
│   ├── install.sh              # 空白 Debian 一键安装
│   └── cdn-agent.service       # systemd 服务文件
└── templates/                  # Nginx 配置模板
    ├── nginx_base.conf.j2      # 主配置模板
    ├── edge_site.conf.j2       # 反代站点 server 块模板（含 WebSocket / 缓存 / HTTPS 跳转）
    └── redirect_site.conf.j2   # 301 重定向站点模板
```

---

## 快速开始

### 一键安装 Manager（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/Daiyuhan223/Daiyuhan-CDN/main/dyh-cdn.sh | bash
```

脚本会自动从 GitHub 拉取最新代码（GitHub 不可达时回退到镜像 zip），安装依赖、启动服务，全程自动。完成后：

```bash
cd /opt/cdn-manager && python3 manager/cli.py site list
```

### 手动部署 Manager

```bash
cd /opt/cdn-manager
chmod +x setup.sh
./setup.sh
```

脚本自动安装依赖、启动服务，完成后自毁。

### 面板登录

访问 `http://<ip>:8080/cdn-gui/`，默认账号：

| 项 | 值 |
|----|----|
| 用户名 | `Admin` |
| 密码 | `cdn123456` |

> 修改密码：编辑 `/opt/cdn-manager/data/panel.conf`（`user=` / `pass=`），或设置环境变量 `CDN_PANEL_USER` / `CDN_PANEL_PASS`，改后重启 `cdn-manager` 生效。

### 添加站点

```bash
# 精确域名
python3 manager/cli.py site add -d www.example.com -o https://origin.example.com

# 通配符域名（必须加引号）
python3 manager/cli.py site add -d "*.example.com" -o https://origin.example.com

# 指定 TTL / 禁用 SSL / http自动跳https
python3 manager/cli.py site add -d static.example.com -o https://origin.example.com --ttl 7200 --no-ssl
python3 manager/cli.py site add -d www.example.com -o https://origin.example.com --force-https

# 301 重定向站点（访客访问 old.example.com 会 301 跳到 new.example.com，保留路径和查询串）
python3 manager/cli.py site add -d old.example.com --redirect https://new.example.com

# 反代站点转重定向 / 重定向站点转反代
python3 manager/cli.py site update -d old.example.com --redirect https://new.example.com
python3 manager/cli.py site update -d old.example.com --no-redirect -o https://origin.example.com
```

> **301 重定向说明：** 填写 `redirect_url` 后该站点不再反代，直接返回 301，且自动关闭缓存。目标不带协议时自动按访问协议（`$scheme`）跳转。重定向站点如需 https 跳转，同样要先给它签发/导入证书。
> **HTTPS 跳转：** 站点开 `--force-https`（或面板"HTTP 自动跳转 HTTPS"）后，http 请求会 301 到 https，需该站点先绑定证书。

> **通配符说明：** `*.example.com` 覆盖所有子域名，`proxy_cache_key` 用 `$host` 区分不同子域名的缓存。
> Shell 里 `*` 必须加引号：`"*.example.com"`。

新站点自动分配给所有已存在的边缘节点，无需手动分配。

### 签发 SSL

```bash
# acme.sh 签发
python3 manager/cli.py ssl issue -d "*.example.com"

# 自签名（测试）
python3 manager/cli.py ssl issue -d "*.example.com" --self-signed

# 从外部导入（allinssl / acme.sh 目录）
python3 manager/cli.py ssl import -d "*.example.com" --cert-dir /root/.acme.sh/*.example.com_ecc

# 从文件导入
python3 manager/cli.py ssl import -d "*.example.com" --cert /path/fullchain.pem --key /path/privkey.pem

# 面板端粘贴 PEM 内容导入
# 面板 → SSL 证书 → 导入 → 方式选"粘贴内容"
```

> 导入时自动校验证书 SAN/CN 是否匹配域名，防止证书串位。
> 支持 `fullchain.pem`/`privkey.pem`、`cert.pem`/`key.pem` 等多种命名。

### 部署边缘节点

在 Manager 上生成注册 Token（自动检测公网 IP）：

```bash
python3 manager/cli.py node token -n <节点名>
```

输出带公网 IP 的安装命令，在边缘节点执行即可。脚本自动：安装 nginx.org 官方 Nginx → 部署 Agent → 注册 → 拉取全量配置。

### 配置 DNS

将加速域名 CNAME 到 CDN 调度域名，通过 GeoDNS 路由用户到最近边缘节点。

---

## 节点类型

系统支持两种边缘节点，由 `cdn_agent.py` 自动适配：

### Auto 节点（空白 Debian）

| 项 | 说明 |
|----|------|
| 适用 | 空白 Debian 11/12/13，root 权限 |
| Nginx | nginx.org 官方源（最新稳定版） |
| 配置路径 | `/etc/nginx/conf.d/cdn/*.conf` |
| SSL 路径 | `/etc/nginx/ssl/<domain>/` |
| 缓存路径 | `/var/cache/nginx/cdn_cache` |
| 部署 | `node token` → 执行输出的 `curl ... install.sh` |

### Baota 节点（宝塔面板）

| 项 | 说明 |
|----|------|
| 适用 | 已装宝塔面板，Nginx 由宝塔管理 |
| Nginx | 宝塔自带（不动宝塔配置） |
| 配置路径 | `/www/server/panel/vhost/nginx/cdn-*.conf` |
| SSL 路径 | `/www/server/nginx/conf/ssl/<domain>/` |
| 缓存路径 | `/www/server/nginx/cdn_cache` |
| 特性 | 只注入独立缓存 zone 文件，不覆盖宝塔 `nginx.conf`，只清理自己的 `cdn-*.conf` |

**Baota 节点创建：**

```bash
# CLI
python3 manager/cli.py node add -n baota-01 -i <IP> --type baota --baota-url http://<IP>:8888 --baota-key <API_KEY>

# 或面板 → 节点管理 → 添加节点 → 类型选 baota → 填 Baota 面板 URL + API Key
```

**Baota 节点部署 Agent：**

推荐直接用注册 Token 一键安装（`install.sh` 会自动识别宝塔，不安装系统 Nginx、不抢占 80/443）：

```bash
# Manager 上生成 token 后，在节点执行输出的 curl 命令即可
```

手动部署 Agent（Agent 仅依赖 Python 标准库，**无需 pip / requests**）：

```bash
python3 /opt/cdn-agent/cdn_agent.py --register --manager http://<manager_ip>:8080 --token <TOKEN>
python3 /opt/cdn-agent/cdn_agent.py --once
python3 /opt/cdn-agent/cdn_agent.py --diag   # 查看探测到的环境（宝塔/系统 nginx 路径）
```

> **注意：** Agent 通过 **宝塔 Nginx 二进制与配置**是否真实存在来判定环境（不再只看 `/www/server/panel` 目录）；优先采用节点注册时在 Manager 上设置的类型。缓存 zone 以独立文件 `00-cdn-cache.conf` 注入宝塔 vhost 目录，不手改宝塔主配置。Baota 面板的安全入口需填在 `baota-url` 里（如 `http://IP:8888/abc123`）。

### 已部署节点升级 Agent

新版 `cdn_agent.py` 带**每小时自更新**机制。手动立即更新：

```bash
systemctl stop cdn-agent
curl -sSL http://<manager_ip>:8080/api/v1/agent/download -o /opt/cdn-agent/cdn_agent.py
systemctl start cdn-agent
python3 /opt/cdn-agent/cdn_agent.py --once
```

---

## CLI 命令

### 站点

```bash
python3 manager/cli.py site list
python3 manager/cli.py site add -d <域名> -o <源站URL> [--ttl <秒>] [--no-ssl] [--force-https] [--cert <证书ID>]
python3 manager/cli.py site add -d <域名> --redirect <目标URL> [--no-ssl]
python3 manager/cli.py site info -d <域名>
python3 manager/cli.py site update -d <域名> [--origin <新源站>] [--ttl <秒>] [--enable|--disable] [--redirect <URL>|--no-redirect] [--cache|--no-cache] [--https|--no-https] [--cert <ID>|--no-cert] [--custom-server-file <文件>] [--custom-location-file <文件>] [--clear-custom]
python3 manager/cli.py site delete -d <域名>
```

### 节点

```bash
python3 manager/cli.py node list
python3 manager/cli.py node add -n <名称> -i <IP> --type auto|baota [--baota-url <URL>] [--baota-key <KEY>]
python3 manager/cli.py node update -n <原名称> [--new-name <新名称>] [-i <新IP>] [-t auto|baota] [--baota-url <URL>] [--baota-key <KEY>]
python3 manager/cli.py node delete -n <名称>
python3 manager/cli.py node token -n <名称> [--type auto|baota]
```

### 证书库

证书集中存放在**证书库**，站点通过 ID 绑定；同一张（含通配符）证书可复用于多个站点。未绑定证书的站点在生成配置时按域名自动匹配库中同名/通配证书。

```bash
python3 manager/cli.py ssl list                            # 查看证书库
python3 manager/cli.py ssl issue -d <域名> [--name <名称>] [--self-signed]   # 签发并入库，自动绑定同名站点
python3 manager/cli.py ssl info -d <域名>                  # 查看站点/同名证书
python3 manager/cli.py ssl import -d <域名> --cert-dir <目录> [--name <名称>]
python3 manager/cli.py ssl import -d <域名> --cert <证书路径> --key <密钥路径> [--name <名称>]
python3 manager/cli.py ssl sync-dir --dir <目录> [--no-bind]   # 扫描目录(AllinSSL部署)自动导入+绑定
python3 manager/cli.py ssl dedup                            # 合并重复证书（保留绑定最多的）
python3 manager/cli.py site update -d <域名> --cert <证书ID>   # 手动绑定
python3 manager/cli.py site update -d <域名> --no-cert         # 解绑
```

### SSL

```bash
python3 manager/cli.py ssl list
python3 manager/cli.py ssl issue -d <域名> [--name <证书库名称>] [--self-signed]
python3 manager/cli.py ssl info -d <域名>
python3 manager/cli.py ssl import -d <域名> [--name <证书库名称>] --cert-dir <目录>
python3 manager/cli.py ssl import -d <域名> [--name <证书库名称>] --cert <证书路径> --key <密钥路径>
```

### 其他

```bash
python3 manager/cli.py config -n <节点名>      # 预览节点配置
python3 manager/cli.py tokens                  # 查看注册 Token
```

> `cache purge` 已移除（nginx.org 官方 Nginx 无 `ngx_cache_purge` 模块），清缓存直接删文件（见缓存管理）。

---

## Web 管理面板

访问 `http://<ip>:8080/cdn-gui/`，需登录。

| 页面 | 功能 |
|------|------|
| 仪表盘 | 站点/节点/证书数量统计 + Manager 状态 + 节点验证状态 |
| 站点管理 | 添加/编辑/删除站点，搜索过滤 |
| 节点管理 | 添加/删除节点（auto/baota），一键生成安装命令，验证状态 |
| SSL 证书 | 签发/导入（目录/文件/粘贴内容）/续期，到期提醒 |

节点列表的"验证"列显示 🟢 已验证 / 🟡 待验证——节点成功应用配置并通过 `sync_token` 回传后变绿。

导航栏显示当前版本，并提供 **「检查更新」** 按钮：对比仓库 `VERSION`/`CHANGELOG.md`，有新版本时导航栏出现"有新版本 vX"徽标；点击弹出更新窗口（版本号 + 更新内容），提供 **「立即更新」**（服务端直接下载并升级，无需 SSH）与 **「忽略本次」**（该版本不再提示，出现更新版本时再提示）。无外网时静默失败，不影响使用。

---

## Agent 特性

| 特性 | 说明 |
|------|------|
| 零依赖 | 仅用 Python 标准库（urllib），无需 pip / requests，规避 PEP668 / distutils 问题 |
| 环境探测 | 依据宝塔 Nginx 二进制/配置真实存在判定环境，优先采用 Manager 上注册的节点类型 |
| 正确管控 | 按环境使用正确的 Nginx 二进制与配置做 `-t`/reload/start，绝不误操作系统 Nginx |
| 备份回滚 | 每次应用新配置前自动备份；`nginx -t` 失败自动回滚上次可用配置（备份/回滚只针对本 agent 管理的文件，不碰宝塔原有配置与证书） |
| 证书全量同步 | 证书变更时删除本 agent 管理的证书目录并全量重传；宝塔仅动带 `.cdn-agent` 标记的目录，绝不动宝塔自带证书 |
| 同步验证 | 配置应用且 **nginx 真正在运行**后才回传 `sync_token`，否则上报 `nginx_down` |
| 自愈 | 每轮轮询检测 nginx 未运行且配置存在时自动拉起 |
| 空间清理 | 磁盘高水位时清理 nginx 日志 / journald / apt 临时文件 / 旧备份 |
| 自更新 | 每小时从 Manager 检查自身版本，不同则自动替换并重启 |
| 站点配置增量 | 站点 conf 按内容哈希只更新变更的文件 |
| 环境诊断 | `cdn_agent.py --diag` 打印探测到的环境与路径 |

---

## 缓存管理

### 缓存存储

- Auto 节点：`/var/cache/nginx/cdn_cache/`
- Baota 节点：`/www/server/nginx/cdn_cache/`

### 清除缓存

由于 nginx.org 官方 Nginx 不带 `ngx_cache_purge` 模块，清除缓存需在节点上手动执行：

```bash
# 全量清除
rm -rf /var/cache/nginx/cdn_cache/*   # Auto 节点
rm -rf /www/server/nginx/cdn_cache/*  # Baota 节点
nginx -s reload
```

### 缓存状态响应头

| 值 | 含义 |
|----|------|
| `HIT` | 命中缓存，直接从边缘节点返回 |
| `MISS` | 未命中，已回源抓取 |
| `EXPIRED` | 缓存已过期，已回源刷新 |
| `STALE` | 源站不可用，返回过期缓存 |

---

## SSL 证书管理

### 证书流程

```
添加站点 → 签发/导入证书 → 存入 SQLite → Agent 同步时分发到节点
                                        ↓
                 Auto: /etc/nginx/ssl/<domain>/
                 Baota: /www/server/nginx/conf/ssl/<domain>/
                                        ↓
                 nginx -t → nginx -s reload
```

> SSL 路径由 `nginx_gen.py` 根据节点类型自动生成。

### 通配符证书

通配符域名（`*.example.com`）SSL 证书仅支持 DNS 验证方式签发。需配置 acme.sh 的 DNS API 凭据（`Ali_Key`/`Ali_Secret` 或 `CF_Token`/`CF_Email`）。

### 从 allinssl / 外部导入

支持 `--cert-dir` 自动发现或 `--cert`/`--key` 指定文件，面板支持直接粘贴 PEM 内容。导入时校验 SAN/CN 与域名匹配，并提示自签证书。

### 对接 AllinSSL 自动部署

用 AllinSSL 的**现成部署方式**把证书落到 Manager 的一个目录，CDN 扫描导入并自动绑定同名/通配站点（无需给 AllinSSL 写插件）。

**1) AllinSSL 用「远程部署 (SSH)」推到 Manager**（推荐）
- 目标：Manager 服务器，SSH 用户 root
- 保存目录：如 `/opt/cdn-manager/data/allinssl`（可含域名子目录）
- 证书/私钥：`fullchain.pem` / `privkey.pem`
- **部署后执行命令**：
  ```bash
  python3 /opt/cdn-manager/manager/cli.py ssl sync-dir --dir /opt/cdn-manager/data/allinssl
  ```
  续签成功后立即导入 + 绑定 + 触发节点热更（60s 内生效）。

**2) AllinSSL 是 Docker、用「本地部署」写卷**
- 把宿主机目录挂进容器，例如：`-v /opt/allinssl-out:/app/deploy`
- AllinSSL 本地部署目标路径填 `/app/deploy`（会写到宿主机 `/opt/allinssl-out`）
- Manager 宿主机加定时扫描（兜底，例如每 5 分钟）：
  ```bash
  echo '*/5 * * * * root python3 /opt/cdn-manager/manager/cli.py ssl sync-dir --dir /opt/allinssl-out >/var/log/cdn-cert-sync.log 2>&1' \
    | tee /etc/cron.d/cdn-cert-sync
  ```

**行为说明**
- 递归扫描目录，自动配对 `fullchain/cert/chain` 与 `privkey/.key`
- 以证书 CN/SAN 自动识别域名；内容无变化则跳过（不触发无谓 reload）
- 仅对**未绑定证书**的站点自动绑定；已显式绑定其他证书的站点不覆盖
- 手动执行：`python3 manager/cli.py ssl sync-dir --dir <目录> [--no-bind]`
- **面板操作**：Web 面板 →「证书库」→ 填目录 →「同步证书目录」按钮（留空使用默认 `data/allinssl`）
- **全自动**：Manager 后台默认每 5 分钟扫描 `CDN_CERT_SYNC_DIR`，自动导入新证书、自动合并删除重复条目（无需手动点按钮）。可用 `CDN_CERT_AUTOSYNC=0` 关闭、`CDN_CERT_AUTOSYNC_INTERVAL` 调间隔。

---

## WebSocket 支持

`templates/edge_site.conf.j2` 已配置 WebSocket 透传：

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

普通 HTTP/HTTPS 请求不受影响，`ws://`/`wss://` 连接可正常代理到源站。

---

## 自定义站点 Nginx 配置

生成的站点 conf 由 Manager 统管并随同步下发，**直接在节点上改会被 agent 覆盖**。要自定义，请在 Manager 侧给站点写入自定义片段（持久化、随配置下发）：

- **自定义 Server 配置**：追加到 `server {}` 末尾，可加 `location`、`rewrite`、`deny/allow`、`add_header` 等
- **自定义 location / 配置**：追加到 `location / {}` 末尾，可调 `proxy_set_header`、`proxy_read_timeout`、`proxy_buffering` 等

面板：站点管理 → 编辑 → 两个自定义文本框。
CLI：把片段写到文件再导入。

```bash
# server 级（例：把 /api 反代到内部服务）
cat > /tmp/site-extra.conf <<'EOF'
location /api/ {
    proxy_pass http://10.0.0.8:9000;
    proxy_set_header Host $host;
}
EOF
python3 manager/cli.py site update -d www.example.com --custom-server-file /tmp/site-extra.conf

# location / 级（例：调大超时、加请求头）
echo 'proxy_read_timeout 300s;' > /tmp/site-loc.conf
python3 manager/cli.py site update -d www.example.com --custom-location-file /tmp/site-loc.conf

# 清除自定义
python3 manager/cli.py site update -d www.example.com --clear-custom
```

> 注意：片段会**原样追加**，别与生成配置里已有的单值指令重复（如重复 `proxy_cache`、重复定义同名 `location`），否则 `nginx -t` 会报错，agent 会回滚并上报 `nginx_down`。先在节点用 `nginx -t` 验证或改完观察面板节点状态。

---

## 配置说明

所有配置集中在 `manager/config.py`，支持环境变量覆盖：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CDN_MANAGER_HOST` | `0.0.0.0` | Manager 监听地址 |
| `CDN_MANAGER_PORT` | `8080` | Manager 端口 |
| `CDN_DB_PATH` | `./data/cdn.db` | 数据库路径 |
| `CDN_PANEL_USER` | `Admin` | 面板用户名 |
| `CDN_PANEL_PASS` | `cdn123456` | 面板密码 |
| `ACME_SH_PATH` | `/root/.acme.sh/acme.sh` | acme.sh 路径 |
| `ACME_DNS_PLUGIN` | `dns_ali` | DNS 验证插件 |
| `Ali_Key` / `Ali_Secret` | - | 阿里云 DNS API |
| `CF_Token` / `CF_Email` | - | Cloudflare DNS API |
| `CDN_CERT_SYNC_DIR` | `./data/allinssl` | AllinSSL 等外部证书部署目录（自动扫描） |
| `CDN_CERT_AUTOSYNC` | `1` | 是否后台自动同步证书目录并自动合并重复（`0` 关闭） |
| `CDN_CERT_AUTOSYNC_INTERVAL` | `300` | 自动同步间隔（秒） |

Nginx 缓存参数：`keys_zone=10m`、`max_size=1g`、`inactive=7d`。

---

## 无损升级

**面板一键升级（最省事）**：Web 面板导航栏出现"有新版本"徽标 → 点开 →「立即更新」。服务端会用独立 systemd 临时单元执行升级（重启 Manager 也不会中断升级过程），完成后页面自动刷新。

**命令行在线一键升级**——自动拉取最新代码并升级，无需手动下载：

```bash
curl -fsSL https://raw.githubusercontent.com/Daiyuhan223/Daiyuhan-CDN/main/upgrade.sh | bash

# 升级到指定 tag / 分支
curl -fsSL https://raw.githubusercontent.com/Daiyuhan223/Daiyuhan-CDN/main/upgrade.sh | bash -s -- v0.1.1
```

**本地升级**——已有新版代码在服务器上时：

```bash
cd /tmp
unzip -qo dyh-cdn.zip -d /opt/cdn-update     # 或 git clone 到服务器
cd /opt/cdn-update
chmod +x update.sh
bash update.sh
```

升级流程会：备份当前代码（可回滚）→ 停服同步代码（排除 `data/`）→ Python 语法自检（失败自动还原）→ 重启服务 → 健康检查 → 清空节点 hash 触发全量重同步。边缘节点 agent 会在一小时内通过 `/api/v1/agent/download` 自更新。

> 注意：升级请用 `upgrade.sh` / `update.sh`，**不要**重跑 `dyh-cdn.sh`（它会 `rm -rf` 整个安装目录）。

---

## 开源信息

- 项目：**Daiyuhan-CDN** v0.1.1
- 作者：**Daiyuhan** — https://github.com/Daiyuhan223
- 仓库：https://github.com/Daiyuhan223/Daiyuhan-CDN
- 版本规则：`0.1.x` 为增量更新（功能/修复），较大功能升级走 `0.2.0`（详见 [CHANGELOG.md](CHANGELOG.md)）

---

## 故障排查

```bash
# Manager
systemctl status cdn-manager
journalctl -u cdn-manager -f

# Agent
systemctl status cdn-agent
journalctl -u cdn-agent -f
python3 /opt/cdn-agent/cdn_agent.py --once

# 手动触发全量重同步（Manager 上清 hash）
cd /opt/cdn-manager && python3 -c "
from manager.database import Database
db = Database()
for n in db.list_nodes():
    db.update_node_config_hash(n.id, None)
print('Hashes cleared')
"

# 清除缓存
rm -rf /var/cache/nginx/cdn_cache/* && nginx -s reload   # Auto
rm -rf /www/server/nginx/cdn_cache/* && nginx -s reload  # Baota
```
