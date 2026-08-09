import asyncio
import json
import logging
import os
import signal
import subprocess
from datetime import datetime
from typing import Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="Iwara Spider Web Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "monitor.log")
DOWNLOADED_IDS_FILE = os.path.join(BASE_DIR, "downloaded_ids.txt")
HIST_JSON = os.path.join(BASE_DIR, "download_history.json")
SPIDER_SCRIPT = os.path.join(BASE_DIR, "iwara_spider.py")
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = "python3"

DEFAULT_CONFIG = {
    "DOWNLOAD_ENGINE": "ytdlp",  # "ytdlp" 或 "aria2"
    "TARGET_URLS": "",           # 自定义爬取网址/API (多条换行)
    "ARIA2_RPC_URL": "http://127.0.0.1:6800/jsonrpc",
    "ARIA2_RPC_TOKEN": "",
    "ARIA2_DOWNLOAD_DIR": "/downloads",
    "YTDLP": "yt-dlp",
    "HEADLESS": True,
    "DOWNLOAD_TIMEOUT": 1800,
    "DURATION_MIN_SEC": 60,
    "DURATION_MAX_SEC": 600,
    "BLOCKED_TITLE_KEYWORDS": ["mmd", "ntr", "dance", "跳舞"],
    "BLOCKED_TAG_IDS": ["blender", "ray_mmd", "monster", "mmd", "dance", "ntr", "hmv"]
}

spider_process: subprocess.Popen = None

def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                merged = DEFAULT_CONFIG.copy()
                merged.update(cfg)
                return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_path = os.path.join(BASE_DIR, "webui.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>WebUI file missing</h1>"

@app.get("/api/status")
async def get_status():
    global spider_process
    is_running = False
    if spider_process is not None:
        if spider_process.poll() is None:
            is_running = True
        else:
            spider_process = None
    
    total_downloaded = 0
    if os.path.exists(DOWNLOADED_IDS_FILE):
        try:
            with open(DOWNLOADED_IDS_FILE, "r", encoding="utf-8") as f:
                total_downloaded = len([l for l in f if l.strip()])
        except Exception:
            pass

    return {
        "running": is_running,
        "total_downloaded": total_downloaded
    }

@app.get("/api/config")
async def get_config():
    return load_config()

@app.post("/api/config")
async def update_config(cfg: Dict[str, Any]):
    save_config(cfg)
    return {"status": "ok", "message": "配置已保存"}

@app.get("/api/history")
async def get_history():
    history = []
    if os.path.exists(HIST_JSON):
        try:
            with open(HIST_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    history = list(reversed(data[-100:]))
        except Exception:
            pass
    
    # 兜底：若 json 不存在，则读取 txt
    if not history and os.path.exists(DOWNLOADED_IDS_FILE):
        try:
            with open(DOWNLOADED_IDS_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
                for line in reversed(lines[-50:]):
                    history.append({"id": line, "title": f"视频 [{line}]", "time": "已完成", "engine": "未知"})
        except Exception:
            pass

    return {"history": history}

@app.post("/api/spider/start")
async def start_spider():
    global spider_process
    if spider_process is not None and spider_process.poll() is None:
        return {"status": "ok", "message": "爬虫已在运行中"}
    
    cfg = load_config()
    env = os.environ.copy()
    venv_bin = os.path.join(BASE_DIR, "venv", "bin")
    env["PATH"] = venv_bin + os.path.pathsep + env.get("PATH", "")
    env["BASE_DIR"] = BASE_DIR
    env["DOWNLOAD_ENGINE"] = str(cfg.get("DOWNLOAD_ENGINE", "ytdlp"))
    env["TARGET_URLS"] = str(cfg.get("TARGET_URLS", ""))
    env["ARIA2_RPC_URL"] = str(cfg.get("ARIA2_RPC_URL", ""))
    env["ARIA2_RPC_TOKEN"] = str(cfg.get("ARIA2_RPC_TOKEN", ""))
    env["ARIA2_DOWNLOAD_DIR"] = str(cfg.get("ARIA2_DOWNLOAD_DIR", ""))
    
    ytdlp_path = str(cfg.get("YTDLP", "yt-dlp"))
    if not os.path.isabs(ytdlp_path):
        venv_ytdlp = os.path.join(venv_bin, "yt-dlp")
        if os.path.exists(venv_ytdlp):
            ytdlp_path = venv_ytdlp
    env["YTDLP"] = ytdlp_path
    env["HEADLESS"] = "1" if cfg.get("HEADLESS", True) else "0"
    env["DOWNLOAD_TIMEOUT"] = str(cfg.get("DOWNLOAD_TIMEOUT", 1800))
    env["DURATION_MIN_SEC"] = str(cfg.get("DURATION_MIN_SEC", 60))
    env["DURATION_MAX_SEC"] = str(cfg.get("DURATION_MAX_SEC", 600))
    env["BLOCKED_TITLE_KEYWORDS"] = json.dumps(cfg.get("BLOCKED_TITLE_KEYWORDS", []))
    env["BLOCKED_TAG_IDS"] = json.dumps(cfg.get("BLOCKED_TAG_IDS", []))
    
    spider_process = subprocess.Popen(
        [VENV_PYTHON, SPIDER_SCRIPT],
        cwd=BASE_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return {"status": "ok", "message": "爬虫服务启动成功"}

@app.post("/api/spider/stop")
async def stop_spider():
    global spider_process
    if spider_process is not None and spider_process.poll() is None:
        spider_process.terminate()
        try:
            spider_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            spider_process.kill()
        spider_process = None
        return {"status": "ok", "message": "爬虫已停止"}
    return {"status": "ok", "message": "爬虫未在运行"}

@app.post("/api/logs/clear")
async def clear_logs():
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 日志已被用户清空\n")
        return {"status": "ok", "message": "日志文件已清空"}
    except Exception as e:
        return {"status": "error", "message": f"清空日志失败: {str(e)}"}

@app.post("/api/test/aria2")
async def test_aria2():
    cfg = load_config()
    url = cfg.get("ARIA2_RPC_URL", "")
    token = cfg.get("ARIA2_RPC_TOKEN", "")
    if not url:
        return {"status": "error", "message": "Aria2 RPC URL 未配置"}
    
    params = []
    if token:
        params.append(f"token:{token}")
    
    payload = {
        "jsonrpc": "2.0",
        "id": "test",
        "method": "aria2.getVersion",
        "params": params
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                if "result" in data:
                    ver = data["result"].get("version", "未知")
                    return {"status": "ok", "message": f"Aria2 连接成功！版本: {ver}"}
                else:
                    return {"status": "error", "message": f"RPC 返回异常: {data}"}
            else:
                return {"status": "error", "message": f"HTTP 错误码: {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "message": f"连接失败: {str(e)}"}

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write("[System] 日志文件初始化...\n")
        
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines[-100:]:
                await websocket.send_text(line)
            
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    await websocket.send_text(line)
                else:
                    await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
