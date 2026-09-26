# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

## [v0.0.1] - 2026-09-13

首个公开版本。

### 新增
- **Manager 控制面**：Flask API + SQLite，集中管理站点、节点、证书、注册 Token。
- **边缘 Agent**：每 60s 轮询同步，配置备份/`nginx -t` 失败自动回滚，自更新，自动适配 Baota 节点。
- **Nginx 配置生成**：Jinja2 模板，支持通配符域名、HTTPS 回源、WebSocket 透传。
- **证书库**：证书集中存放，站点通过 ID 绑定；支持通配符复用；旧版按站点证书自动迁移；导入时校验 SAN/CN。
- **301 重定向站点**：站点直接返回 301 跳转，保留路径与查询串，自动关闭缓存。
- **HTTP → HTTPS 自动跳转**：站点级开关。
- **缓存管理**：`proxy_cache`，HIT/MISS/EXPIRED/STALE 状态头；带 Cookie / Authorization 的请求自动跳过缓存，避免登录态串缓存。
- **节点空间自动清理**：磁盘高水位时清理 nginx 日志、journald、apt/临时文件、旧备份。
- **节点自愈**：Agent 检测到 nginx 未运行且配置存在时自动拉起。
- **Web 管理面板**：仪表盘、站点、节点、证书库；表单校验与操作反馈；节点支持添加/编辑/删除。
- **交互式引导 `dyh-cdn`** 与 **CLI**（`site` / `node` / `ssl` / `tokens` / `config`）。
- **一键部署/升级/卸载脚本**：`setup.sh`、`dyh-cdn.sh`、`update.sh`、`uninstall.sh`。

### 修复（Baota 混合环境）
- `install.sh` 安装前真实探测宝塔环境；宝塔机不再安装系统 Nginx，并停用冲突的 `nginx.service`，避免抢 80/443。
- Agent 环境探测改为依据宝塔 Nginx 二进制/配置是否真实存在，并优先采用 Manager 上注册的节点类型，杜绝配置写错目录。
- Agent 按环境使用正确的 Nginx 二进制做 `nginx -t` / reload / start（宝塔不再误用系统 `systemctl`）。
- 宝塔缓存 zone 改为独立 include 文件注入，不再手改宝塔主配置，修复 `zone "cdn_cache" is unknown`。
- 备份/回滚重构，修复宝塔站点目录 basename 不匹配导致回滚失效的问题。
- Agent 去掉 `requests`，仅用标准库 `urllib`，根治 Debian 12 PEP668 拦截、Debian 11 污染系统 Python、`idna.core` 缺失与 pip 死循环。
- 应用配置后校验 Nginx 真实运行状态，未运行不上报"已验证"，并上报 `nginx_down` 状态。
- Manager 依赖改为 **apt 优先安装**（`python3-flask/python3-jinja2/python3-requests`），避免 PEP668 拦截与系统 Python 污染，同时保留 pip 兜底。

### 说明
- `cache purge` 未提供：nginx.org 官方 Nginx 无 `ngx_cache_purge` 模块，清缓存请直接删缓存文件。

[v0.0.1]: https://github.com/Daiyuhan223/Daiyuhan-CDN/releases/tag/v0.0.1
