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

### 说明
- `cache purge` 未提供：nginx.org 官方 Nginx 无 `ngx_cache_purge` 模块，清缓存请直接删缓存文件。

[v0.0.1]: https://github.com/Daiyuhan223/Daiyuhan-CDN/releases/tag/v0.0.1
