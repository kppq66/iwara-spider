# Iwara Video Monitoring & Automation Dashboard

基于 Python Playwright / yt-dlp / Aria2 RPC 与 FastAPI 打造的 Iwara 自动化视频监控、解析过滤与图形化控制面板。支持本地极速下载与远程 Aria2 离线下载的无缝切换。

## ✨ 特性
- 🎨 **清爽绿白 Dashboard**：集成 Vue 3 + Tailwind CSS，移动端自适应响应式布局。
- ⚡ **双引擎支持**：
  - **yt-dlp 本地模式**（默认）：配合 `curl-cffi` 绕过 Cloudflare 校验，无需本地配置 Aria2。
  - **Aria2 RPC 模式**：一键连接远端/本地 Aria2 服务端离线推流。
- 🔍 **灵活的目标筛选**：支持自动扫描 Trending 页面或指定 URL 列表。
- 🛡️ **智能过滤系统**：自定义标题/标签黑名单与时长过滤。
- 📊 **结构化下载历史**：可视化查看视频 ID、格式标题、下载时间与调用的引擎。
- 📜 **实时日志流与切割**：支持 WebSocket 极速日志流推送、自动轮转切割（5MB/副本）及网页端物理清空。

## 🚀 快速开始

### 1. 安装依赖
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 启动 Dashboard
```bash
python3 app.py
```
默认在浏览器访问：`http://localhost:8000/` 即可控制爬虫拉起、配置参数与查看历史。

### 3. Systemd 常驻后台服务（可选）
写入 `/etc/systemd/system/iwara-web.service`:
```ini
[Unit]
Description=Iwara Spider Web Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/iwara-spider
ExecStart=/path/to/iwara-spider/venv/bin/python3 app.py
Restart=always

[Install]
WantedBy=multi-user.target
```
启动服务：
```bash
systemctl daemon-reload
systemctl enable --now iwara-web.service
```
