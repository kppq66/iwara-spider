import asyncio
import os
import re
import subprocess
import sys
import json
import shlex
import posixpath
import time
import logging
import shutil
from logging.handlers import RotatingFileHandler
from datetime import datetime
import httpx
from urllib.parse import urlparse
from playwright.async_api import async_playwright

# ============ 基础配置（支持环境变量配置） ============
BASE_DIR = os.environ.get("BASE_DIR", os.path.dirname(os.path.abspath(__file__)))

H = os.path.join(BASE_DIR, "downloaded_ids.txt")
HIST_JSON = os.path.join(BASE_DIR, "download_history.json")
D = os.environ.get("DOWNLOAD_LOCAL_DIR", os.path.join(BASE_DIR, "videos"))
LOG_FILE = os.path.join(BASE_DIR, "monitor.log")
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
BROWSER_PROFILE_DIR = os.path.join(BASE_DIR, "browser_profile")

# 下载引擎模式: "ytdlp" (默认，直接本地下载) 或 "aria2" (使用 Aria2 RPC)
DOWNLOAD_ENGINE = os.environ.get("DOWNLOAD_ENGINE", "ytdlp").lower()

# 自定义目标爬取网址 (逗号或换行分隔，若留空则自动按 Trending 0-8 页扫描)
TARGET_URLS_RAW = os.environ.get("TARGET_URLS", "").strip()

# Aria2 RPC 配置 (仅当 DOWNLOAD_ENGINE == "aria2" 时生效)
ARIA2_RPC_URL = os.environ.get("ARIA2_RPC_URL", "http://127.0.0.1:6800/jsonrpc")
ARIA2_RPC_TOKEN = os.environ.get("ARIA2_RPC_TOKEN", "")
ARIA2_DOWNLOAD_DIR = os.environ.get("ARIA2_DOWNLOAD_DIR", "/downloads")

YTDLP_ENV = os.environ.get("YTDLP", "")
if YTDLP_ENV and shutil.which(YTDLP_ENV):
    YTDLP = YTDLP_ENV
else:
    venv_ytdlp = os.path.join(BASE_DIR, "venv", "bin", "yt-dlp")
    if os.path.exists(venv_ytdlp):
        YTDLP = venv_ytdlp
    else:
        YTDLP = shutil.which("yt-dlp") or "yt-dlp"

# 是否显示浏览器窗口 (无头模式)
HEADLESS = os.environ.get("HEADLESS", "1") == "1"

# 单次下载超时时间（秒）
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 1800))

# 视频时长限制（秒）：1～10 分钟
DURATION_MIN_SEC = int(os.environ.get("DURATION_MIN_SEC", 60))
DURATION_MAX_SEC = int(os.environ.get("DURATION_MAX_SEC", 600))

# 标题命中以下任意关键词则跳过（不区分大小写）
raw_title_kw = os.environ.get("BLOCKED_TITLE_KEYWORDS", "")
if raw_title_kw:
    try:
        BLOCKED_TITLE_KEYWORDS = json.loads(raw_title_kw)
    except Exception:
        BLOCKED_TITLE_KEYWORDS = [x.strip() for x in raw_title_kw.split(",") if x.strip()]
else:
    BLOCKED_TITLE_KEYWORDS = ["mmd", "ntr", "dance", "跳舞"]

# Tag 命中以下任意标签则跳过（不区分大小写）
raw_tag_kw = os.environ.get("BLOCKED_TAG_IDS", "")
if raw_tag_kw:
    try:
        BLOCKED_TAG_IDS = set(json.loads(raw_tag_kw))
    except Exception:
        BLOCKED_TAG_IDS = {x.strip().lower() for x in raw_tag_kw.split(",") if x.strip()}
else:
    BLOCKED_TAG_IDS = {"blender", "ray_mmd", "monster", "mmd", "dance", "ntr", "hmv"}

os.makedirs(D, exist_ok=True)
os.makedirs(DEBUG_DIR, exist_ok=True)
os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)

# ============ 日志配置 (轮转日志: 单个最大 5MB, 最多保留 3 个备份) ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("iwara_spider")

# 历史记录加载
hist = set(open(H).read().splitlines()) if os.path.exists(H) else set()

def record_history(v_id: str, title: str, engine: str):
    """记录已下载视频信息（同步存入 txt 与 structured json）"""
    hist.add(v_id)
    with open(H, "a", encoding="utf-8") as f:
        f.write(f"{v_id}\n")
    
    history_records = []
    if os.path.exists(HIST_JSON):
        try:
            with open(HIST_JSON, "r", encoding="utf-8") as f:
                history_records = json.load(f)
        except Exception:
            history_records = []
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history_records.append({
        "id": v_id,
        "title": title or "未知标题",
        "time": now_str,
        "engine": engine
    })
    
    with open(HIST_JSON, "w", encoding="utf-8") as f:
        json.dump(history_records, f, ensure_ascii=False, indent=2)

def short_text(text, limit=120):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit] + "..."

def short_url(url, limit=160):
    return short_text(url, limit)

def parse_t(s):
    if not s:
        return 0
    if isinstance(s, int) or (isinstance(s, str) and str(s).isdigit()):
        return int(s)
    p = list(map(int, str(s).split(':')))
    return p[0] * 60 + p[1] if len(p) == 2 else (p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else 0)

def fmt_duration(seconds):
    if not seconds:
        return "未知"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def is_duration_ok(dur_str):
    if not dur_str:
        return True
    return DURATION_MIN_SEC <= parse_t(dur_str) <= DURATION_MAX_SEC

def is_title_blocked(text):
    if not text:
        return False
    t_lower = text.lower()
    return any(k.lower() in t_lower for k in BLOCKED_TITLE_KEYWORDS)

def get_blocked_tags(tags):
    if not tags or not isinstance(tags, list):
        return []
    hit = []
    for tag in tags:
        t_id = tag.get("id") if isinstance(tag, dict) else str(tag)
        if t_id and str(t_id).lower() in BLOCKED_TAG_IDS:
            hit.append(t_id)
    return hit

def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "Video")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120]

# ============ Aria2 RPC 工具 ============
async def aria2_rpc_call(method, params):
    log.info(f"[aria2] RPC 调用: {method}")
    payload = {"jsonrpc": "2.0", "id": "iwara_spider", "method": method, "params": params}
    if ARIA2_RPC_TOKEN:
        payload["params"] = [f"token:{ARIA2_RPC_TOKEN}"] + payload["params"]

    last_err = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(ARIA2_RPC_URL, json=payload)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP 代码 {r.status_code}: {r.text}")
            data = r.json()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data.get("result")
        except Exception as e:
            last_err = e
            log.warning(f"[aria2] RPC 网络异常: method={method} | attempt={attempt}/3 | err={e}")
            if attempt < 3:
                await asyncio.sleep(attempt)
    raise last_err

async def download_with_aria2_rpc(url, output_name, referer=None):
    options = {
        "dir": ARIA2_DOWNLOAD_DIR,
        "out": output_name,
        "continue": "true",
        "max-connection-per-server": "8",
        "split": "8",
        "min-split-size": "1M",
        "max-tries": "10",
        "retry-wait": "3",
        "timeout": "30",
        "file-allocation": "none",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if referer:
        options["referer"] = referer

    async def _try_once():
        gid = await aria2_rpc_call("aria2.addUri", [[url], options])
        log.info(f"[aria2] 已提交任务: gid={gid} | out={output_name} | url={short_url(url)}")
        deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT
        while True:
            status = await aria2_rpc_call(
                "aria2.tellStatus", [gid, ["status", "errorMessage", "completedLength", "totalLength"]]
            )
            state = status.get("status")
            if state == "complete":
                log.info(f"[aria2] 下载成功: out={output_name}")
                return True, ""
            if state in {"error", "removed"}:
                errmsg = status.get("errorMessage") or state
                log.warning(f"[STREAM] aria2 任务失败: gid={gid} | state={state} | error={errmsg}")
                return False, errmsg
            if asyncio.get_running_loop().time() >= deadline:
                await aria2_rpc_call("aria2.remove", [gid])
                log.error(f"[STREAM] aria2 任务超时已移除: {gid}")
                return False, "timeout"
            await asyncio.sleep(5)

    try:
        ok, errmsg = await _try_once()
        return ok
    except Exception as e:
        log.warning(f"[STREAM] aria2 RPC 发生异常: {e}")
        return False

# ============ yt-dlp 直线与直载 ============
async def resolve_with_ytdlp(page_url, referer=None):
    common = [
        YTDLP,
        "--no-playlist",
        "-f", "Source/best",
        "--impersonate", "Chrome-120",
        "--legacy-server-connect",
        "--socket-timeout", "30",
        "--retries", "3",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    if referer:
        common += ["--add-header", f"Referer: {referer}"]
    log.info(f"[yt-dlp] 解析直链中: page={short_url(page_url)}")
    try:
        probe_cmd = common + ["--simulate", "--print", "format_id", "--print", "urls", page_url]
        probe = await asyncio.to_thread(subprocess.run, probe_cmd, capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            log.warning(f"[yt-dlp] 解析失败: code={probe.returncode} | stderr={short_text(probe.stderr, 300)}")
            return "", ""
        lines = [x.strip() for x in (probe.stdout or "").splitlines() if x.strip()]
        if len(lines) < 2:
            return "", ""
        fmt = short_text(lines[0], 80)
        resolved_url = lines[-1]
        log.info(f"[yt-dlp] 解析成功: format_id={fmt} | resolved={short_url(resolved_url)}")
        return resolved_url, fmt
    except Exception as e:
        log.warning(f"[yt-dlp] 解析异常: {e}")
        return "", ""

async def download_with_ytdlp_direct(page_url, v_id, custom_title=None, referer=None):
    """默认模式：用 yt-dlp 直接下载到本地 videos/ 目录，不经过 Aria2"""
    safe_t = safe_filename(custom_title or v_id)
    out_template = os.path.join(D, f"{safe_t} [{v_id}].%(ext)s")
    cmd = [
        YTDLP,
        "--no-playlist",
        "-f", "Source/best",
        "--impersonate", "Chrome-120",
        "--legacy-server-connect",
        "--socket-timeout", "30",
        "--retries", "5",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "-o", out_template,
        page_url
    ]
    if referer:
        cmd += ["--add-header", f"Referer: {referer}"]
    log.info(f"[yt-dlp 本地下载] 开始下载: {v_id} -> {out_template}")
    try:
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        if proc.returncode == 0:
            log.info(f"[yt-dlp 本地下载] 下载成功: {v_id}")
            return True
        else:
            log.warning(f"[yt-dlp 本地下载] 失败 code={proc.returncode}: {short_text(proc.stderr, 300)}")
            return False
    except Exception as e:
        log.warning(f"[yt-dlp 本地下载] 异常: {e}")
        return False

async def download_video(url, v_id, custom_title=None, referer=None, duration_str=None, page_url=None):
    dur_display = duration_str if duration_str else fmt_duration(None)
    title_display = custom_title or v_id
    
    if DOWNLOAD_ENGINE == "ytdlp":
        # 默认模式: yt-dlp 本地直接下载
        target_page = page_url or f"https://www.iwara.tv/video/{v_id}"
        if await download_with_ytdlp_direct(target_page, v_id, custom_title=title_display, referer=referer):
            record_history(v_id, title_display, "yt-dlp")
            log.info(f"[yt-dlp] 视频任务下载完成: {v_id} | 标题: {title_display[:40]}")
            return True
        return False
    else:
        # Aria2 RPC 下载模式
        base_name = safe_filename(title_display)
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            ext = ".mp4"
        output_name = f"{base_name} [{v_id}]{ext}"
        log.info(f"[aria2 下载] 开始提交: {v_id} | 时长: {dur_display} | out={output_name}")
        if await download_with_aria2_rpc(url, output_name, referer=referer):
            record_history(v_id, title_display, "aria2")
            log.info(f"[aria2] 视频任务下载完成: {v_id} | 标题: {title_display[:40]}")
            return True
        return False

async def sniff_stream_url(page, play_url, attempts=3):
    last_title = "Video"
    for attempt in range(1, attempts + 1):
        found = None
        fallback = None

        def handle_res(res):
            nonlocal found, fallback
            if (".m3u8" in res.url or ".mp4" in res.url) and not found:
                if any(x in res.url.lower() for x in ["thumb", "poster", "preview", "avatar"]):
                    return
                if "filesq.iwara.tv" in res.url and re.search(r"/file/[0-9a-fA-F-]{36}", res.url):
                    fallback = res.url
                    return
                found = res.url

        page.on("response", handle_res)
        try:
            await page.goto("about:blank", timeout=10000, wait_until="domcontentloaded")
            await page.goto(play_url, timeout=60000, wait_until="domcontentloaded")
            last_title = await page.title()
            for _ in range(25):
                if found:
                    break
                await page.wait_for_timeout(500)
                try:
                    last_title = await page.title()
                except Exception:
                    pass
                if "Just a moment" not in last_title and "Cloudflare" not in last_title:
                    break
            if found:
                return found, last_title
        except Exception as e:
            log.warning(f"[sniff] 播放页嗅探失败: attempt={attempt}/{attempts} | {e}")
        finally:
            page.remove_listener("response", handle_res)
        if fallback:
            return fallback, last_title
        if attempt < attempts:
            await page.wait_for_timeout(3000 * attempt)
    return None, last_title

# ============ Iwara API 扫描逻辑 ============
def get_target_list_urls():
    if TARGET_URLS_RAW:
        urls = [x.strip() for x in re.split(r"[\n,]", TARGET_URLS_RAW) if x.strip()]
        if urls:
            return urls
    return [
        f"https://api.iwara.tv/videos?sort=trending&page={page}&limit=24"
        for page in range(0, 9)
    ]

_IWARA_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.iwara.tv/",
}

async def resolve_iwara_file_url(file_url, label="fileUrl"):
    if not file_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30, headers=_IWARA_API_HEADERS, follow_redirects=True) as client:
            r = await client.get(file_url)
        if r.status_code != 200:
            return ""
        sources = r.json()
    except Exception as e:
        log.warning(f"[Iwara API] fileUrl解析失败: {short_url(file_url)} | {e}")
        return ""
    if not isinstance(sources, list):
        return ""

    def quality(item):
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        m = re.search(r"\d+", name)
        if m:
            return int(m.group(0))
        if name.lower() not in ("preview", ""):
            return 99999
        return 0

    formal = [x for x in sources if isinstance(x, dict) and str(x.get("name") or "").lower() != "preview"]
    source_items = formal or [x for x in sources if isinstance(x, dict)]

    for item in sorted(source_items, key=quality, reverse=True):
        src = item.get("src") or {}
        candidate = src.get("download") or src.get("view") or ""
        if not candidate:
            continue
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        return candidate
    return ""

def fmt_api_duration(seconds):
    if seconds is None:
        return None
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return None
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"

async def warmup_iwara_home(page):
    try:
        await page.goto("https://www.iwara.tv/", timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        title = await page.title()
        log.info(f"[Iwara warmup] 首页预热完成: title={short_text(title)}")
    except Exception as e:
        log.warning(f"[Iwara warmup] 首页预热失败: {e}")

async def scan_iwara_api(page):
    target_urls = get_target_list_urls()
    log.info(f"正在扫描 Iwara (引擎: {DOWNLOAD_ENGINE.upper()} | 目标入口数: {len(target_urls)})...")
    await warmup_iwara_home(page)

    merged = {}
    async with httpx.AsyncClient(timeout=30, headers=_IWARA_API_HEADERS, follow_redirects=True) as client:
        for list_url in target_urls:
            try:
                r = await client.get(list_url)
            except Exception as e:
                log.warning(f"[Iwara API] 请求失败 {list_url}: {e}")
                continue
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue

            results = data.get("results", []) if isinstance(data, dict) else []
            for item in results:
                if isinstance(item, dict) and item.get("id"):
                    merged[item["id"]] = item

    if not merged:
        log.warning("[Iwara API] 本轮未扫描到符合格式的 API 结果。")
        return

    raw_list = list(merged.values())
    n_iw = s_iw = 0
    for item in raw_list:
        v_id = item.get("id")
        if not v_id:
            continue

        title = item.get("title", "")
        slug = item.get("slug", "")
        page_url = f"https://www.iwara.tv/video/{v_id}/{slug}"
        tags = item.get("tags", [])

        blocked_tags = get_blocked_tags(tags)
        if is_title_blocked(title) or blocked_tags:
            log.info(f"[Iwara API] 跳过（命中黑名单 {blocked_tags}）: {v_id} | {title[:60]}")
            continue

        dur_sec = (item.get("file") or {}).get("duration")
        dur_str = fmt_api_duration(dur_sec)
        if not is_duration_ok(dur_str):
            log.info(f"[Iwara API] 跳过（时长 {dur_str or '未知'} 不在范围）: {v_id}")
            continue

        if v_id in hist:
            s_iw += 1
            continue

        page_title = title

        # 若配置为 yt-dlp 直接下载引擎，直接触发
        if DOWNLOAD_ENGINE == "ytdlp":
            if await download_video("", v_id, custom_title=page_title, referer=page_url, duration_str=dur_str, page_url=page_url):
                n_iw += 1
                continue

        # Aria2 模式下：优先解析直链提交
        file_url = item.get("fileUrl") or ""
        ytdlp_url, ytdlp_fmt = await resolve_with_ytdlp(page_url, referer=page_url)
        if ytdlp_url:
            if await download_video(ytdlp_url, v_id, custom_title=page_title, referer=page_url, duration_str=dur_str, page_url=page_url):
                n_iw += 1
                continue

        # 次链路：Playwright 嗅探播放页
        stream_url, sniff_title = await sniff_stream_url(page, page_url)
        if sniff_title and sniff_title != "Video":
            page_title = sniff_title

        is_filesq = bool(stream_url and "filesq.iwara.tv" in stream_url and re.search(r"/file/[0-9a-fA-F-]{36}", stream_url))
        if is_filesq:
            resolved = await resolve_iwara_file_url(stream_url, label="filesq清单")
            if resolved:
                stream_url = resolved

        if not stream_url and file_url:
            stream_url = await resolve_iwara_file_url(file_url)

        if not stream_url:
            log.warning(f"[Iwara API] 未获取到媒体直链: {v_id}")
            continue

        if is_title_blocked(page_title):
            continue

        if await download_video(stream_url, v_id, custom_title=page_title, referer=page_url, duration_str=dur_str, page_url=page_url):
            n_iw += 1

    log.info(f"[Iwara API] 候选 {len(raw_list)} 个，已存在 {s_iw} 个，新下载 {n_iw} 个。")

# ============ 主循环 ============
async def check():
    log.info(f"Spider 启动: Engine={DOWNLOAD_ENGINE.upper()} | 本地输出目录={D}")
    if DOWNLOAD_ENGINE == "aria2":
        log.info(f"Aria2 RPC 配置: {ARIA2_RPC_URL} | 远端目录: {ARIA2_DOWNLOAD_DIR}")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=HEADLESS,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3]});"
            "Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en']});"
            "Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });"
        )

        page = await ctx.new_page()

        while True:
            log.info("=" * 40)
            log.info("开始执行 Iwara 循环扫描...")

            try:
                await scan_iwara_api(page)
            except Exception as e:
                log.exception(f"[scan_iwara_api] 捕获未处理异常: {e}")

            log.info("本轮扫描结束，休眠 1800 秒后开启下一轮...")
            await asyncio.sleep(1800)

if __name__ == "__main__":
    try:
        asyncio.run(check())
    except KeyboardInterrupt:
        log.info("收到中断信号，程序正常退出。")
