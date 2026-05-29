"""
财联社电报监控 GUI 应用
基于 PyQt6 的桌面应用，包装 cls_telegraph.py 的核心逻辑。

依赖:
    pip install PyQt6 selenium webdriver-manager pandas openpyxl
运行:
    python3 cls_app.py
打包:
    pyinstaller --onefile --windowed --name "财联社监控" cls_app.py
"""
from __future__ import annotations

import os
import re
import json
import time
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QSpinBox, QAbstractSpinBox, QLineEdit, QPushButton, QTextEdit,
    QTableWidget, QTableWidgetItem, QTabWidget, QFileDialog, QDialog, QComboBox,
    QHeaderView, QSplitter, QStatusBar, QFrame, QListWidget,
    QGraphicsOpacityEffect,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QPoint,
)
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QClipboard, QPixmap


# ──────────────────────────────────────────
# 常量
# ──────────────────────────────────────────

URL = "https://www.cls.cn/telegraph"
API_URL = "https://www.cls.cn/api/cache?app=CailianpressWeb&name=telegraphList&os=web&sv=8.7.9"
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.cls.cn/telegraph",
}


class _RateLimitError(Exception):
    """API 速率限制异常，用于中断批量分析"""

COLUMNS = [
    "ID", "发布时间", "标题", "内容",
    "相关股票", "股票代码", "AI分析", "抓取时间", "AI分析时间",
]
COL_WIDTHS = {
    "ID": 36, "发布时间": 22, "标题": 40, "内容": 80,
    "相关股票": 30, "股票代码": 25, "AI分析": 80,
    "抓取时间": 22, "AI分析时间": 22,
}

AI_PROMPT = """你是专业的A股市场分析师。分析以下财联社新闻，判断对A股上市公司的影响。

规则：
1. 只关注A股（沪深两市），不含港股/美股
2. 股票代码必须是6位数字
3. sentiment字段只能是"利好"或"利空"
4. 若无相关A股，stocks为空数组
5. summary不超过50字

严格返回JSON，不要有任何其他文字：
{"stocks":[{"code":"6位代码","name":"股票名","sentiment":"利好|利空","reason":"原因"}],"summary":"摘要"}"""

DEFAULTS = {
    "interval_min": 5,
    "scroll_times": 3,
    "wait_timeout": 20,
    "excel_path": str(Path.home() / "cls_telegraph.xlsx"),
    "analyze_all": True,
    "claude_bin": "",
    "chrome_bin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "watch_codes": [],   # 自选股代码列表
    "quote_refresh_secs": 30,  # 报价刷新间隔（秒）
    # AI 提供商配置
    "ai_provider": "claude_cli",   # claude_cli / claude_api / openai / gemini / kimi / qwen / custom
    "ai_api_key": "",
    "ai_model": "",
    "ai_base_url": "",
}

QUOTE_REFRESH_SECS = 30  # 报价刷新间隔（秒）

# 极简调色盘：1 主色 + 3 中性色
COLOR_BG        = "#FFFFFF"   # background
COLOR_SURFACE   = "#F7F7F7"   # subtle surface (sidebar)
COLOR_TEXT      = "#1A1A1A"   # primary text
COLOR_MUTED     = "#8E8E93"   # secondary text
COLOR_ACCENT    = "#007AFF"   # single CTA accent
COLOR_BORDER    = "#E5E5E7"   # input border only
COLOR_SEL       = "#EBEBEB"   # selection highlight（中性灰，不用蓝色）
COLOR_ROW_SEP   = "#F0F0F0"   # 表格行间分隔线

# 旧名别名（保持下方代码引用不变）
COLOR_PANEL     = "#FFFFFF"
COLOR_ELEVATED  = "#F0F0F0"
COLOR_GREEN     = "#34C759"   # A股绿跌
COLOR_GREEN_DIM = "#E8F8ED"
COLOR_RED       = "#FF3B30"   # A股红涨
COLOR_RED_DIM   = "#FDECEA"
COLOR_BLUE      = "#007AFF"
COLOR_BLUE_DIM  = "#EAF2FF"
COLOR_ORANGE    = "#8E8E93"
COLOR_INPUT_BG  = "#FFFFFF"
COLOR_EXEC_DIM  = "#F7F7F7"


# ──────────────────────────────────────────
# ConfigManager
# ──────────────────────────────────────────

class ConfigManager:
    CONFIG_PATH = Path.home() / ".cls_monitor_config.json"

    @classmethod
    def load(cls) -> dict:
        if cls.CONFIG_PATH.exists():
            try:
                with open(cls.CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cfg = dict(DEFAULTS)
                cfg.update(data)
                return cfg
            except Exception:
                pass
        return dict(DEFAULTS)

    @classmethod
    def save(cls, cfg: dict):
        try:
            with open(cls.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存配置失败: {e}")

    @classmethod
    def detect_claude_bin(cls) -> str:
        # 1. shutil.which
        found = shutil.which("claude")
        if found:
            return found

        # 2. npm global
        npm_paths = [
            Path.home() / ".npm-global" / "bin" / "claude",
            Path("/usr/local/bin/claude"),
            Path("/usr/bin/claude"),
        ]
        for p in npm_paths:
            if p.exists():
                return str(p)

        # 3. homebrew
        brew_paths = [
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/opt/claude/bin/claude"),
        ]
        for p in brew_paths:
            if p.exists():
                return str(p)

        # 4. nvm / fnm 常见路径
        nvm_base = Path.home() / ".nvm" / "versions" / "node"
        if nvm_base.exists():
            for node_ver in sorted(nvm_base.iterdir(), reverse=True):
                candidate = node_ver / "bin" / "claude"
                if candidate.exists():
                    return str(candidate)

        return "claude"


# ──────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_driver(config: dict) -> webdriver.Chrome:
    # 使用独立目录，避免 Chrome 触碰受保护目录
    chrome_data_dir = Path.home() / ".cls_monitor_chrome"
    chrome_data_dir.mkdir(exist_ok=True)
    download_dir = chrome_data_dir / "downloads"
    download_dir.mkdir(exist_ok=True)
    # 清理残留锁文件，防止上次异常退出导致 Chrome 无法启动
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"]:
        lock_path = chrome_data_dir / lock
        if lock_path.exists() or lock_path.is_symlink():
            lock_path.unlink(missing_ok=True)

    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--mute-audio")
    opts.add_argument("--use-fake-ui-for-media-stream")
    opts.add_argument(f"--user-data-dir={chrome_data_dir}")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("prefs", {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "safebrowsing.enabled": False,
        "profile.default_content_setting_values": {
            "media_stream_camera": 2,
            "media_stream_mic": 2,
            "geolocation": 2,
            "notifications": 2,
            "midi_sysex": 2,
        },
    })
    chrome_bin = config.get("chrome_bin", "")
    if chrome_bin and Path(chrome_bin).exists():
        opts.binary_location = chrome_bin
    return webdriver.Chrome(
        service=Service(_get_chromedriver()),
        options=opts,
    )


def _get_chromedriver() -> str:
    """
    获取可用的 chromedriver 路径。
    优先用 ChromeDriverManager 下载/定位，若启动后立即被系统杀掉（exit -9/137），
    则从本地缓存中找到能正常运行的版本。
    """
    import subprocess as _sp
    import glob as _glob

    def _is_working(path: str) -> bool:
        try:
            r = _sp.run([path, "--version"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    # 先尝试 webdriver-manager 推荐版本
    try:
        path = ChromeDriverManager().install()
        if _is_working(path):
            return path
    except Exception:
        pass

    # 回退：扫描本地缓存，找第一个可用的
    cache_pattern = str(Path.home() / ".wdm/drivers/chromedriver/mac64/*/chromedriver-mac-arm64/chromedriver")
    candidates = sorted(_glob.glob(cache_pattern), reverse=True)  # 版本号降序
    for path in candidates:
        if _is_working(path):
            return path

    raise RuntimeError("未找到可用的 chromedriver，请检查 Chrome 安装或网络连接")


# ──────────────────────────────────────────
# API 抓取（优先使用，比 Selenium 更稳定）
# ──────────────────────────────────────────

def _fetch_telegraph_api(log_fn=None) -> list[dict]:
    """通过 API 获取电报数据，返回与 parse_page 相同格式的 list[dict]"""
    try:
        r = requests.get(API_URL, headers=API_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("errno") != 0:
            if log_fn:
                log_fn(f"[{now()}] API 返回错误: errno={data.get('errno')}", "error")
            return []
        roll_data = data.get("data", {}).get("roll_data", [])
        if not roll_data:
            return []
        return _parse_api_items(roll_data)
    except requests.RequestException as e:
        if log_fn:
            log_fn(f"[{now()}] API 请求失败: {e}", "error")
    except Exception as e:
        if log_fn:
            log_fn(f"[{now()}] API 解析失败: {e}", "error")
    return []


def _parse_api_items(roll_data: list) -> list[dict]:
    """将 API 返回的 roll_data 转换为与 parse_page 兼容的格式"""
    results = []
    for item in roll_data:
        ctime = item.get("ctime", 0)
        pub_time = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M:%S") if ctime else ""
        brief = item.get("brief", "") or item.get("content", "") or ""
        content = item.get("content", "") or brief

        m = re.match(r"^(【[^】]+】)(.*)", brief, re.DOTALL)
        title = m.group(1) if m else (item.get("title", "") or "")
        body = m.group(2).strip() if m else brief

        uid = str(item.get("id", f"{pub_time}_{brief[:20]}"))
        results.append({
            "ID": uid, "发布时间": pub_time, "标题": title, "内容": body,
            "抓取时间": now(),
            "相关股票": "", "股票代码": "", "AI分析": "", "AI分析时间": "",
        })
    return results


def parse_page(driver: webdriver.Chrome, log_fn=None) -> list[dict]:
    results = []
    today = datetime.now().strftime("%Y-%m-%d")
    time_pat = re.compile(
        r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?|\d{2}:\d{2}(?::\d{2})?)$"
    )

    selectors = [
        ".telegraph-content-box", ".telg-item",
        "[class*='telegraph'] li", "[class*='roll-item']",
        "[class*='news-item']", "article",
    ]
    items = []
    for sel in selectors:
        items = driver.find_elements(By.CSS_SELECTOR, sel)
        if items:
            break

    if not items:
        if log_fn:
            log_fn(f"[{now()}] 未找到电报条目，请检查页面结构", "error")
        return results

    for el in items:
        try:
            text = el.text.strip()
            if not text:
                continue
            lines = text.splitlines()
            if lines and time_pat.match(lines[0].strip()):
                raw_time = lines[0].strip()
                pub_time = (
                    f"{today} {raw_time}"
                    if re.match(r"^\d{2}:\d{2}", raw_time)
                    else raw_time
                )
                content_lines = lines[1:]
            else:
                pub_time = ""
                content_lines = lines
            content = " ".join(l.strip() for l in content_lines if l.strip())
            m = re.match(r"^(【[^】]+】)(.*)", content, re.DOTALL)
            title = m.group(1) if m else ""
            body = m.group(2).strip() if m else content
            uid = f"{pub_time}_{content[:20]}"
            results.append({
                "ID": uid, "发布时间": pub_time, "标题": title, "内容": body,
                "抓取时间": now(),
                "相关股票": "", "股票代码": "", "AI分析": "", "AI分析时间": "",
            })
        except Exception:
            continue
    return results


def fetch_items(driver: webdriver.Chrome | None, config: dict, log_fn=None) -> list[dict]:
    """获取电报数据 — 优先使用 API，失败时降级为 Selenium 网页抓取"""
    # 优先尝试 API
    items = _fetch_telegraph_api(log_fn)
    if items:
        if log_fn:
            log_fn(f"[{now()}] API 获取到 {len(items)} 条电报", "normal")
        return items

    # API 失败，降级为 Selenium 抓取
    if log_fn:
        log_fn(f"[{now()}] API 失败，降级为网页抓取...", "error")
    if driver is None:
        return []
    driver.get(URL)
    wait_timeout = config.get("wait_timeout", 20)
    scroll_times = config.get("scroll_times", 3)
    try:
        WebDriverWait(driver, wait_timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR,
                 ".telegraph-list,.telg-list,[class*='roll'],[class*='telegraph']")
            )
        )
    except Exception:
        pass
    for _ in range(scroll_times):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
    return parse_page(driver, log_fn)


def _analyze_with_claude_cli(news_text: str, config: dict, log_fn=None) -> dict | None:
    """使用 Claude CLI 分析新闻"""
    claude_bin = config.get("claude_bin", "") or "claude"
    # 配置路径不存在时自动探测
    if not Path(claude_bin).is_file():
        fallback = ConfigManager.detect_claude_bin()
        if log_fn and claude_bin != "claude":
            log_fn(f"[{now()}] claude 路径 {claude_bin} 不存在，改用 {fallback}", "error")
        claude_bin = fallback
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    bin_dir = str(Path(claude_bin).parent)
    current_path = env.get("PATH", "")
    if bin_dir not in current_path:
        env["PATH"] = bin_dir + os.pathsep + current_path

    try:
        result = subprocess.run(
            [claude_bin, "-p", AI_PROMPT, "--output-format", "json"],
            input=news_text,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if result.returncode != 0:
            if log_fn:
                log_fn(f"[{now()}] CLI 错误: {result.stderr[:100]}", "error")
            return None
        outer = json.loads(result.stdout)
        raw = outer.get("result", "")
        json_match = re.search(r"\{[\s\S]+\}", raw)
        if not json_match:
            return None
        return json.loads(json_match.group())
    except subprocess.TimeoutExpired:
        if log_fn:
            log_fn(f"[{now()}] AI 分析超时，跳过", "error")
        return None
    except json.JSONDecodeError as e:
        if log_fn:
            log_fn(f"[{now()}] JSON 解析失败: {e}", "error")
        return None
    except Exception as e:
        if log_fn:
            log_fn(f"[{now()}] AI 分析异常: {e}", "error")
        return None


def _analyze_with_kiro_cli(news_text: str, config: dict, log_fn=None) -> dict | None:
    """使用 kiro-cli chat --no-interactive 分析新闻"""
    kiro_bin = shutil.which("kiro-cli") or "/Users/10259879/.local/bin/kiro-cli"
    prompt = AI_PROMPT + "\n\n" + news_text
    env = os.environ.copy()
    try:
        result = subprocess.run(
            [kiro_bin, "chat", "--no-interactive", "--trust-tools="],
            input=prompt,
            capture_output=True, text=True, timeout=90, env=env,
        )
        if result.returncode != 0:
            if log_fn:
                log_fn(f"[{now()}] kiro-cli 错误: {result.stderr[:100]}", "error")
            return None
        # 清理 ANSI 转义码
        raw = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).strip()
        # 逐行查找第一个以 { 开头的完整 JSON 对象
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        # fallback: 正则提取
        json_match = re.search(r"\{[^{}]*\"stocks\"[^{}]*\[[\s\S]*?\]\s*,\s*\"summary\"[^{}]*\}", raw)
        if json_match:
            return json.loads(json_match.group())
        if log_fn:
            log_fn(f"[{now()}] kiro-cli 未返回有效 JSON: {raw[:150]}", "error")
        return None
    except subprocess.TimeoutExpired:
        if log_fn:
            log_fn(f"[{now()}] kiro-cli 超时，跳过", "error")
        return None
    except Exception as e:
        if log_fn:
            log_fn(f"[{now()}] kiro-cli 异常: {e}", "error")
        return None


# AI 提供商默认参数
_AI_PROVIDER_DEFAULTS = {
    "kiro_cli":    {"base_url": "",                                                   "model": ""},
    "claude_cli":  {"base_url": "",                                                   "model": ""},
    "claude_api":  {"base_url": "https://api.anthropic.com",                          "model": "claude-3-5-sonnet-20241022"},
    "openai":      {"base_url": "https://api.openai.com/v1",                          "model": "gpt-4o-mini"},
    "gemini":      {"base_url": "",                                                   "model": "gemini-1.5-flash"},
    "kimi":        {"base_url": "https://api.moonshot.cn/v1",                         "model": "moonshot-v1-8k"},
    "qwen":        {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",  "model": "qwen-turbo"},
    "custom":      {"base_url": "",                                                   "model": ""},
}


def _analyze_with_api(news_text: str, config: dict, log_fn=None) -> dict | None:
    """通过 HTTP API 调用 AI（支持 OpenAI 兼容格式、Gemini、Anthropic Claude API）"""
    import requests as _req
    provider = config.get("ai_provider", "openai")
    api_key  = config.get("ai_api_key", "")
    model    = config.get("ai_model", "") or _AI_PROVIDER_DEFAULTS.get(provider, {}).get("model", "")
    base_url = config.get("ai_base_url", "") or _AI_PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "")

    try:
        if provider == "gemini":
            # Google Gemini REST API
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model or 'gemini-1.5-flash'}:generateContent?key={api_key}"
            )
            data = {
                "contents": [{"parts": [{"text": AI_PROMPT + "\n\n" + news_text}]}],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "application/json",  # 强制 JSON 输出
                },
            }
            r = _req.post(url, json=data, timeout=60)
            r.raise_for_status()
            content = r.json()["candidates"][0]["content"]["parts"][0]["text"]

        elif provider == "claude_api":
            # Anthropic Claude API
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            data = {
                "model": model or "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "system": AI_PROMPT,
                "messages": [{"role": "user", "content": news_text}],
            }
            url = base_url.rstrip("/") + "/v1/messages"
            r = _req.post(url, headers=headers, json=data, timeout=60)
            r.raise_for_status()
            content = r.json()["content"][0]["text"]

        else:
            # OpenAI 兼容格式（openai / kimi / qwen / custom）
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            data = {
                "messages": [
                    {"role": "system", "content": AI_PROMPT},
                    {"role": "user", "content": news_text},
                ],
                "temperature": 0.3,
                "response_format": {"type": "json_object"},  # 强制 JSON 输出
            }
            if model:
                data["model"] = model
            url = base_url.rstrip("/") + "/chat/completions"
            r = _req.post(url, headers=headers, json=data, timeout=60)
            # 若提供商不支持 response_format，降级重试
            if r.status_code in (400, 422):
                data.pop("response_format", None)
                r = _req.post(url, headers=headers, json=data, timeout=60)
            # 429 限流：等待后重试一次
            if r.status_code == 429:
                if log_fn:
                    log_fn(f"[{now()}] 触发限流(429)，等待 60 秒后重试...", "error")
                time.sleep(60)
                r = _req.post(url, headers=headers, json=data, timeout=60)
                if r.status_code == 429:
                    raise _RateLimitError("API 速率限制持续，本批分析已暂停")
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]

        # 打印原始回复（调试用）
        if log_fn:
            log_fn(f"  [API 原始回复] {content[:300]}", "normal")

        # 去除 markdown 代码块（```json ... ``` 或 ``` ... ```）
        content = re.sub(r"```(?:json)?\s*", "", content)
        content = re.sub(r"```", "", content).strip()

        # 先尝试整体解析
        try:
            result = json.loads(content)
            if log_fn:
                log_fn(f"  [API 解析成功] stocks={len(result.get('stocks', []))} summary={str(result.get('summary',''))[:40]}", "normal")
            return result
        except json.JSONDecodeError:
            pass

        # 再用正则提取第一个 JSON 对象
        json_match = re.search(r"\{[\s\S]+\}", content)
        if not json_match:
            if log_fn:
                log_fn(f"[{now()}] AI 未返回有效 JSON，原始: {content[:200]}", "error")
            return None
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError as e:
            if log_fn:
                log_fn(f"[{now()}] JSON 解析失败: {e} | 原始: {content[:200]}", "error")
            return None

    except _RateLimitError:
        raise   # 速率限制异常向上传播，由 enrich_with_ai 处理
    except Exception as e:
        if log_fn:
            log_fn(f"[{now()}] API 调用失败: {e}", "error")
        return None


def analyze_news(title: str, body: str, config: dict, log_fn=None) -> dict | None:
    news_text = f"{title}{body}".strip()
    if not news_text:
        return None

    provider = config.get("ai_provider", "claude_cli")
    if provider == "kiro_cli":
        return _analyze_with_kiro_cli(news_text, config, log_fn)
    elif provider == "claude_cli":
        return _analyze_with_claude_cli(news_text, config, log_fn)
    else:
        return _analyze_with_api(news_text, config, log_fn)


def format_stocks(analysis: dict | None, analyze_all: bool) -> tuple[str, str, str]:
    """返回 (相关股票显示名, 股票代码(换行分隔), AI分析详情)"""
    if not analysis:
        return "", "", ""

    stocks = analysis.get("stocks") or []
    if not stocks:
        if analyze_all:
            return "无相关股票", "", analysis.get("summary", "")
        return "", "", ""

    # 按利好/利空分组，名称前加箭头
    names_parts = []
    for s in stocks:
        sentiment = s.get("sentiment", "")
        arrow = "↑" if sentiment == "利好" else "↓" if sentiment == "利空" else "→"
        names_parts.append(f"{s.get('name', '')}{arrow}")
    names = "\n".join(names_parts)

    # 股票代码每个单独一行，方便双击复制
    codes = "\n".join(s.get("code", "") for s in stocks if s.get("code"))

    lines = [
        f"[{s.get('sentiment','')}]【{s.get('name')}({s.get('code')})】{s.get('reason','')}"
        for s in stocks
    ]
    detail = analysis.get("summary", "") + "\n" + "\n".join(lines)
    return names, codes, detail.strip()


def load_existing(path: Path, log_fn=None) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_excel(path, dtype=str)
        except Exception as e:
            if log_fn:
                log_fn(f"[{now()}] 读取 Excel 失败，将重建: {e}", "error")
    return pd.DataFrame()


def save_to_excel(df: pd.DataFrame, path: Path, added: int, total: int, log_fn=None):
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[COLUMNS]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="电报")
        ws = writer.sheets["电报"]
        for col, w in COL_WIDTHS.items():
            if col in df.columns:
                letter = ws.cell(1, df.columns.get_loc(col) + 1).column_letter
                ws.column_dimensions[letter].width = w
        ws.freeze_panes = "A2"
    msg = f"[{now()}] 新增 {added} 条 → 共 {total} 条 | {path}"
    if log_fn:
        log_fn(msg, "normal")


def enrich_with_ai(df: pd.DataFrame, config: dict, log_fn=None, row_fn=None, emit_ids=None) -> pd.DataFrame:
    """AI 批量分析；row_fn(row_dict) 每条分析完后回调（用于实时 emit）"""
    t_empty  = df["AI分析时间"].isna() | (df["AI分析时间"].fillna("") == "")
    ai_empty = df["AI分析"].isna()     | (df["AI分析"].fillna("") == "")
    st_empty = df["相关股票"].isna()   | (df["相关股票"].fillna("") == "")
    # 未分析，或之前分析失败（有时间戳但内容全空）
    mask = t_empty | (ai_empty & st_empty)
    # 若指定了 emit_ids，只分析本次新抓取的条目，旧数据跳过
    if emit_ids is not None:
        mask = mask & df["ID"].isin(emit_ids)
    indices = df.index[mask].tolist()

    if not indices:
        return df

    analyze_all = config.get("analyze_all", True)
    if log_fn:
        log_fn(f"[{now()}] 开始 AI 分析，共 {len(indices)} 条...", "normal")

    for i, idx in enumerate(indices, 1):
        row = df.loc[idx]
        title = str(row.get("标题", "") or "")
        body = str(row.get("内容", "") or "")
        try:
            analysis = analyze_news(title, body, config, log_fn)
        except _RateLimitError as e:
            if log_fn:
                log_fn(f"[{now()}] ⚠ {e}，已分析 {i-1}/{len(indices)} 条，下次运行将继续", "error")
            break

        if analysis is None:
            # 分析失败，不写时间戳，下次运行时重试
            if log_fn:
                log_fn(f"  [{i}/{len(indices)}] {(title or body)[:35]} → 分析失败，跳过", "error")
            time.sleep(2)
            continue

        names, codes, detail = format_stocks(analysis, analyze_all)

        df.at[idx, "相关股票"] = names
        df.at[idx, "股票代码"] = codes
        df.at[idx, "AI分析"] = detail
        df.at[idx, "AI分析时间"] = now()

        has_relevant = bool(names) and names != "无相关股票"
        has_bearish = "↓" in names if has_relevant else False
        has_bullish_flag = "↑" in names if has_relevant else False
        tag = f"✓ {names}" if has_relevant else "- 无相关"
        if has_bearish and not has_bullish_flag:
            level = "error"
        elif has_bullish_flag:
            level = "good"
        else:
            level = "normal"
        if log_fn:
            log_fn(
                f"  [{i}/{len(indices)}] {(title or body)[:35]} → {tag}",
                level,
            )

        # 实时回调（仅限本次新抓取的条目）
        if row_fn:
            updated = df.loc[idx].to_dict()
            if emit_ids is None or updated.get("ID") in emit_ids:
                row_fn([updated])

        if config.get("ai_provider", "claude_cli") not in ("claude_cli", "kiro_cli"):
            time.sleep(22)   # KIMI 免费版约 3 RPM，需 ~20s 间隔

    return df


def job(config: dict, log_fn=None, row_fn=None):
    """主任务：抓取 → 去重 → AI分析 → 保存"""
    excel_path = Path(config.get("excel_path", DEFAULTS["excel_path"]))
    if log_fn:
        log_fn(f"\n[{now()}] ── 开始抓取 ──", "normal")
    driver = None
    added = 0
    total = 0
    try:
        # 优先 API 抓取（无需 driver）
        new_items = fetch_items(None, config, log_fn)
        if not new_items:
            # API 失败，创建 driver 降级到 Selenium
            driver = build_driver(config)
            new_items = fetch_items(driver, config, log_fn)
        if driver:
            driver.quit()
            driver = None

        if not new_items:
            if log_fn:
                log_fn(f"[{now()}] 未获取到数据", "error")
            return 0, 0

        if log_fn:
            log_fn(f"[{now()}] 抓取到 {len(new_items)} 条电报", "normal")

        new_df = pd.DataFrame(new_items)
        old_df = load_existing(excel_path, log_fn)

        old_ids: set = set(old_df["ID"].dropna()) if not old_df.empty and "ID" in old_df.columns else set()
        truly_new_ids: set = set(new_df["ID"].dropna()) - old_ids

        if old_df.empty:
            combined = new_df.copy()
        else:
            for col in new_df.columns:
                if col not in old_df.columns:
                    old_df[col] = ""
            combined = pd.concat([old_df, new_df], ignore_index=True)
            combined.drop_duplicates(subset=["ID"], keep="first", inplace=True)

        added = len(truly_new_ids)
        # 只对真正新的条目触发 row_fn（不弹旧数据）
        combined = enrich_with_ai(combined, config, log_fn, row_fn, emit_ids=truly_new_ids)

        if "发布时间" in combined.columns:
            combined.sort_values("发布时间", ascending=False, inplace=True, ignore_index=True)

        total = len(combined)
        save_to_excel(combined, excel_path, added, total, log_fn)

    except Exception as e:
        if log_fn:
            log_fn(f"[{now()}] 任务异常: {e}", "error")
            log_fn(traceback.format_exc(), "error")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return added, total


# ──────────────────────────────────────────
# QuoteFetchThread — 实时报价
# ──────────────────────────────────────────

def _market_prefix(code: str) -> str:
    """根据代码判断交易所前缀：sh=沪，sz=深"""
    # 沪市指数：000xxx / 000xxx 段中属于上交所的指数代码
    _SH_INDICES = {
        "000001",  # 上证指数
        "000016",  # 上证50
        "000300",  # 沪深300
        "000905",  # 中证500
        "000852",  # 中证1000
        "000010",  # 上证180
        "000688",  # 科创50
        "000906",  # 中证800
        "000985",  # 中证全指
    }
    if code in _SH_INDICES:
        return "sh"
    return "sh" if code.startswith("6") else "sz"


class SearchLineEdit(QLineEdit):
    """支持方向键把焦点移到关联下拉列表的输入框"""
    def __init__(self, suggest_list: "QListWidget", parent=None):
        super().__init__(parent)
        self._suggest_list = suggest_list

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_Down
                and self._suggest_list.isVisible()):
            self._suggest_list.setFocus()
            if self._suggest_list.currentRow() < 0:
                self._suggest_list.setCurrentRow(0)
            return
        if (event.key() == Qt.Key.Key_Escape
                and self._suggest_list.isVisible()):
            self._suggest_list.hide()
            return
        super().keyPressEvent(event)


class QuoteFetchThread(QThread):
    quotes_ready = pyqtSignal(list)   # list[dict]: code/name/price/pct_change

    def __init__(self, codes: list[str]):
        super().__init__()
        self.codes = codes

    def run(self):
        if not self.codes:
            return
        try:
            import requests
            symbols = ",".join(_market_prefix(c) + c for c in self.codes)
            url = f"https://qt.gtimg.cn/q={symbols}"
            r = requests.get(url, timeout=8,
                             headers={"Referer": "https://finance.qq.com"})
            r.encoding = "gbk"
            results = []
            for line in r.text.strip().splitlines():
                # v_sh600036="1~招商银行~600036~39.90~...~0.35~..."
                m = re.match(r'v_[a-z]{2}(\d{6})="([^"]+)"', line)
                if not m:
                    continue
                code   = m.group(1)
                fields = m.group(2).split("~")
                if len(fields) < 33:
                    continue
                results.append({
                    "code":       code,
                    "name":       fields[1],
                    "price":      fields[3],
                    "pct_change": fields[32],
                })
            # 保持原始添加顺序
            order = {c: i for i, c in enumerate(self.codes)}
            results.sort(key=lambda x: order.get(x["code"], 999))
            self.quotes_ready.emit(results)
        except Exception:
            import traceback; traceback.print_exc()


# ──────────────────────────────────────────
# ScraperThread
# ──────────────────────────────────────────

# ──────────────────────────────────────────
# StockListLoader — 后台加载全量股票列表
# ──────────────────────────────────────────

class StockListLoader(QThread):
    loaded = pyqtSignal(list)   # list[dict]: code/name/pinyin

    # 主要指数（手动维护，不依赖 akshare 接口）
    _INDICES = [
        ("000001", "上证指数"),
        ("000016", "上证50"),
        ("000300", "沪深300"),
        ("000905", "中证500"),
        ("000852", "中证1000"),
        ("000688", "科创50"),
        ("000906", "中证800"),
        ("399001", "深证成指"),
        ("399006", "创业板指"),
        ("399005", "中小100"),
        ("399300", "沪深300"),
    ]

    def run(self):
        try:
            import akshare as ak
            from pypinyin import lazy_pinyin, Style
            df = ak.stock_info_a_code_name()
            result = []

            # 先插入指数，确保搜索时排在前面
            for code, name in self._INDICES:
                initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER))
                result.append({"code": code, "name": name, "pinyin": initials.lower()})

            for _, row in df.iterrows():
                name = str(row["name"]).replace(" ", "")
                initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER))
                result.append({
                    "code":   str(row["code"]),
                    "name":   name,
                    "pinyin": initials.lower(),
                })
            self.loaded.emit(result)
        except Exception:
            self.loaded.emit([])


class ScraperThread(QThread):
    log_message  = pyqtSignal(str, str)   # (text, level)
    new_data     = pyqtSignal(list)        # list[dict]
    job_finished = pyqtSignal(int, int)    # (added, total)
    job_error    = pyqtSignal(str)

    def __init__(self, config: dict, mode: str = "loop"):
        super().__init__()
        self.config = config
        self.mode = mode
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self):
        self._stop_flag = False

        def log_fn(text, level="normal"):
            self.log_message.emit(str(text), level)
            print(text, flush=True)   # 同步输出到终端，便于调试

        def row_fn(rows):
            self.new_data.emit(rows)

        if self.mode == "once":
            added, total = job(self.config, log_fn, row_fn)
            self.job_finished.emit(added, total)
            return

        # loop 模式
        interval_sec = self.config.get("interval_min", 5) * 60
        while not self._stop_flag:
            added, total = job(self.config, log_fn, row_fn)
            self.job_finished.emit(added, total)

            # 倒计时等待（每秒检查 stop_flag）
            for _ in range(interval_sec):
                if self._stop_flag:
                    break
                time.sleep(1)

        log_fn(f"[{now()}] 监控已停止", "normal")


# ──────────────────────────────────────────
# IntradayFetchThread — 分时数据获取
# ──────────────────────────────────────────

class IntradayFetchThread(QThread):
    data_ready  = pyqtSignal(list, float)   # (分时条目列表, 昨收价)
    fetch_error = pyqtSignal(str)

    def __init__(self, code: str):
        super().__init__()
        self.code = code

    @staticmethod
    def _em_secid(code: str) -> str:
        _SH_INDICES = {"000001","000016","000300","000905","000852",
                       "000688","000906","000985","000010"}
        if code in _SH_INDICES or code.startswith("6"):
            return f"1.{code}"
        return f"0.{code}"

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }

    def run(self):
        items, pre_price = self._fetch_em()
        if not items:
            items, pre_price = self._fetch_tencent()
        self.data_ready.emit(items, pre_price)

    def _fetch_em(self):
        """东方财富分时 API（主）"""
        try:
            import requests, random
            secid = self._em_secid(self.code)
            url = (
                "https://push2.eastmoney.com/api/qt/stock/trends2/get"
                f"?fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
                f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
                f"&ut=b2884a393a59ad64002292a3e90d46a5"
                f"&nsr=1&secid={secid}&r={random.random()}"
            )
            r = requests.get(url, timeout=10, headers=self._HEADERS)
            obj       = r.json()
            inner     = obj.get("data") or {}
            pre_price = float(inner.get("preClose") or 0)
            trends    = inner.get("trends") or []
            items = []
            for entry in trends:
                parts = entry.split(",")
                if len(parts) < 6:
                    continue
                try:
                    time_str = parts[0].split(" ", 1)[-1]   # "09:30"
                    price    = float(parts[1])
                    volume   = int(float(parts[5]))
                    amount   = float(parts[6]) if len(parts) > 6 else 0.0  # f57 逐分钟成交额
                    api_avg  = float(parts[7]) if len(parts) > 7 else 0.0  # f58 东财预算均价
                    items.append({"time": time_str, "price": price, "volume": volume,
                                  "amount": amount, "api_avg": api_avg})
                except (ValueError, IndexError):
                    continue

            # 东财指数的成交量/成交额字段是累计量，需差分还原为逐分钟量
            if len(items) > 2:
                vols = [d["volume"] for d in items]
                # 若超过一半的相邻值是递增的，认为是累计量
                inc = sum(1 for i in range(1, len(vols)) if vols[i] >= vols[i-1])
                if inc > len(vols) * 0.6:
                    prev_v, prev_a = 0, 0.0
                    for d in items:
                        d["volume"], prev_v = max(0, d["volume"] - prev_v), d["volume"]
                        d["amount"], prev_a = max(0.0, d["amount"] - prev_a), d["amount"]

            return items, pre_price
        except Exception:
            return [], 0.0

    def _fetch_tencent(self):
        """腾讯财经分时 API（备用）"""
        try:
            import requests, random
            prefix    = _market_prefix(self.code)
            full_code = prefix + self.code
            url = (
                f"http://web.ifzq.gtimg.cn/appstock/app/minute/query"
                f"?code={full_code}&r={random.random()}"
            )
            r = requests.get(url, timeout=8,
                             headers={"Referer": "https://finance.qq.com"})
            obj       = r.json()
            inner     = obj.get("data", {}).get(full_code, {}).get("data", {})
            raw_lines = inner.get("data", [])
            pre_price = float(inner.get("pre_price") or 0)
            items = []
            for line in raw_lines:
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    t = parts[0]   # "093000" or "09:30"
                    if ":" not in t and len(t) >= 4:
                        t = f"{t[:2]}:{t[2:4]}"
                    items.append({"time": t, "price": float(parts[1]),
                                  "volume": int(parts[2])})
                except (ValueError, IndexError):
                    continue
            return items, pre_price
        except Exception as e:
            self.fetch_error.emit(str(e))
            return [], 0.0


def _calc_macd(prices: list, fast: int = 6, slow: int = 13, signal: int = 5):
    """计算 MACD(fast, slow, signal)，返回等长列表 (dif, dea, hist)。"""
    n = len(prices)
    if n == 0:
        return [], [], []

    def _ema(src, period):
        k = 2.0 / (period + 1)
        out = [0.0] * len(src)
        out[0] = src[0]
        for i in range(1, len(src)):
            out[i] = src[i] * k + out[i - 1] * (1 - k)
        return out

    fast_ema = _ema(prices, fast)
    slow_ema = _ema(prices, slow)
    dif  = [f - s for f, s in zip(fast_ema, slow_ema)]
    dea  = _ema(dif, signal)
    hist = [2 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, hist


def _time_to_pos(t: str) -> int | None:
    """'HH:MM' → 交易分钟坐标 (0=09:30 … 239=14:59, 240=15:00)；非交易时段返回 None。"""
    try:
        h, m = int(t[:2]), int(t[3:])
        mins = h * 60 + m
        if 570 <= mins <= 690:    # 09:30-11:30
            return mins - 570
        if 780 <= mins <= 900:    # 13:00-15:00
            return 120 + (mins - 780)
        return None
    except Exception:
        return None


def _build_vwap(items: list) -> list:
    """
    构建与同花顺一致的分时均价序列（长度同 items）。
    优先使用 API 直接提供的 api_avg（东财 f58），
    否则用逐分钟成交额 amount 累计除以成交量，
    最后退回 price×volume 近似值。
    """
    n = len(items)
    result = [0.0] * n
    cum_amount = 0.0
    cum_vol    = 0.0
    for i, d in enumerate(items):
        # API 直接给了均价：直接用（跳过累计计算）
        if d.get("api_avg", 0.0) > 0:
            result[i] = d["api_avg"]
            continue
        # 用实际成交额累计
        if _time_to_pos(d["time"]) is not None:
            amt = d.get("amount", 0.0)
            vol = d.get("volume", 0)
            if amt > 0:
                cum_amount += amt
                cum_vol    += vol
            else:
                cum_amount += d["price"] * vol
                cum_vol    += vol
        result[i] = cum_amount / cum_vol if cum_vol else d["price"]
    return result


# ──────────────────────────────────────────
# MultiStockScanner — 后台批量扫描做T信号
# ──────────────────────────────────────────

class MultiStockScanner(QThread):
    """
    对监控栏所有股票（排除当前详细图股票）做后台做T扫描。
    每次仅检测上次扫描后新增的数据点，避免历史重复提醒。
    state 格式：{code: {"data_len": n, "last_buy_idx": i, "last_sell_idx": i}}
    """
    signal_found = pyqtSignal(str, str, float, str)   # code, type, price, time
    scan_done    = pyqtSignal(object)                 # dict: 更新后的 state

    _MIN_GAP = 5

    def __init__(self, codes: list, state: dict, parent=None):
        super().__init__(parent)
        self.codes  = list(codes)
        self._state = {k: dict(v) for k, v in state.items()}
        self._new_state: dict = {}

    def run(self):
        import time as _t
        for code in self.codes:
            try:
                self._scan_one(code)
            except Exception:
                pass
            _t.sleep(0.3)
        self.scan_done.emit(self._new_state)

    def _scan_one(self, code: str):
        t = IntradayFetchThread(code)
        items, _ = t._fetch_em()
        if not items:
            items, _ = t._fetch_tencent()
        n = len(items)
        if n < 14:
            return

        st       = self._state.get(code, {})
        prev_len = st.get("data_len", -1)

        # 首次扫描只初始化状态，不产生提醒（避免历史数据刷屏）
        if prev_len < 0:
            self._new_state[code] = {
                "data_len": n, "last_buy_idx": -self._MIN_GAP, "last_sell_idx": -self._MIN_GAP,
            }
            return

        last_buy_idx  = st.get("last_buy_idx",  -self._MIN_GAP)
        last_sell_idx = st.get("last_sell_idx", -self._MIN_GAP)
        start = max(5, prev_len)   # 只扫描新增数据

        # 防护：若新旧差距过大（初始化时数据少、后来补全），跳过本轮避免历史刷屏
        if n - start > 10:
            self._new_state[code] = {
                "data_len": n,
                "last_buy_idx": last_buy_idx,
                "last_sell_idx": last_sell_idx,
            }
            return

        prices = [d["price"] for d in items]
        dif, dea, _ = _calc_macd(prices)
        vwap = _build_vwap(items)

        scan_start = max(start, 14)
        for i in range(scan_start, n):
            d         = items[i]
            cur_price = d["price"]
            avg_price = vwap[i]

            golden = dif[i-1] <= dea[i-1] and dif[i] > dea[i]
            death  = dif[i-1] >= dea[i-1] and dif[i] < dea[i]

            if _time_to_pos(d["time"]) is None:
                continue

            if golden and dea[i] < 0 and cur_price < avg_price:
                if i - last_buy_idx >= self._MIN_GAP:
                    last_buy_idx = i
                    self.signal_found.emit(code, "buy", cur_price, d["time"])
            elif death and dif[i] > 0 and cur_price > avg_price:
                if i - last_sell_idx >= self._MIN_GAP:
                    last_sell_idx = i
                    self.signal_found.emit(code, "sell", cur_price, d["time"])

        self._new_state[code] = {
            "data_len": n, "last_buy_idx": last_buy_idx, "last_sell_idx": last_sell_idx,
        }


# ──────────────────────────────────────────
# IntradayMonitorTab — 分时 T 做监控 Tab
# ──────────────────────────────────────────

class IntradayMonitorTab(QWidget):
    """
    分时走势图 + MACD(6,13,5) 做T提醒：
      买入：金叉 AND DEA < 0 AND 价格 < 分时均线
      卖出：死叉 AND DIF > 0 AND 价格 > 分时均线
      同一信号 MIN_GAP 根内不重复
    """

    # 向主窗口广播做T信号，供报价胶囊显示 badge（code, "buy"/"sell"）
    signal_triggered = pyqtSignal(str, str)

    # 信号冷却（秒）
    _COOLDOWN = 600

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[dict] = []
        self._pre_price: float = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._fetch)
        self._signal_points: list[dict] = []   # {type, pos, price, time, idx}
        self._current_code: str = ""
        self._canvas = None
        self._fig = None
        # 全股票后台扫描
        self._all_codes: list = []
        self._all_state: dict = {}              # {code: {data_len, last_buy_idx, last_sell_idx}}
        self._all_scanner: MultiStockScanner | None = None
        self._all_timer = QTimer(self)
        self._all_timer.timeout.connect(self._run_all_scan)
        self._build_ui()

    # ── UI 构建 ──────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # ── 控制行
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)

        lbl_stock = QLabel("股票")
        lbl_stock.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 12px;")
        ctrl.addWidget(lbl_stock)

        self.combo_code = QComboBox()
        self.combo_code.setFixedHeight(30)
        self.combo_code.setMinimumWidth(160)
        self.combo_code.setStyleSheet(f"""
            QComboBox {{
                background-color: {COLOR_INPUT_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                color: {COLOR_TEXT};
                font-size: 13px;
            }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox QAbstractItemView {{
                background-color: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                color: {COLOR_TEXT};
                selection-background-color: {COLOR_SEL};
            }}
        """)
        self.combo_code.currentIndexChanged.connect(self._on_code_changed)
        ctrl.addWidget(self.combo_code)

        ctrl.addSpacing(16)
        lbl_interval = QLabel("刷新")
        lbl_interval.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 12px;")
        ctrl.addWidget(lbl_interval)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(5, 300)
        self.spin_interval.setValue(30)
        self.spin_interval.setSuffix(" 秒")
        self.spin_interval.setFixedHeight(30)
        self.spin_interval.setFixedWidth(80)
        self.spin_interval.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_interval.valueChanged.connect(self._on_interval_changed)
        ctrl.addWidget(self.spin_interval)

        self.btn_rescan = QPushButton("重新扫描")
        self.btn_rescan.setFixedHeight(30)
        self.btn_rescan.setToolTip("重新扫描全天 MACD 信号")
        self.btn_rescan.clicked.connect(self._apply_threshold)
        ctrl.addWidget(self.btn_rescan)

        ctrl.addStretch()

        root.addLayout(ctrl)

        # ── matplotlib 图表
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        import matplotlib
        # Arial Unicode MS 在 macOS 上必然存在且完整支持 CJK
        matplotlib.rcParams.update({
            "font.sans-serif":   ["Arial Unicode MS", "PingFang SC", "Heiti SC", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "axes.facecolor":    COLOR_BG,
            "figure.facecolor":  COLOR_BG,
            "text.color":        COLOR_TEXT,
            "axes.labelcolor":   COLOR_MUTED,
            "xtick.color":       COLOR_MUTED,
            "ytick.color":       COLOR_MUTED,
            "axes.edgecolor":    COLOR_BORDER,
            "grid.color":        COLOR_ROW_SEP,
        })
        self._fig = Figure(figsize=(8, 4.5))
        self._fig.subplots_adjust(
            left=0.08, right=0.88, top=0.97, bottom=0.14, hspace=0.12
        )
        self._ax_price  = self._fig.add_subplot(3, 1, (1, 2))
        self._ax_pct    = self._ax_price.twinx()   # 右侧涨跌幅轴，永久保留
        self._ax_macd   = self._fig.add_subplot(3, 1, 3, sharex=self._ax_price)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.setFixedHeight(390)
        root.addWidget(self._canvas)

        # ── 状态标签
        self.lbl_signal = QLabel("")
        self.lbl_signal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_signal.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {COLOR_MUTED};"
            f"padding: 4px; border-radius: 4px;"
        )
        root.addWidget(self.lbl_signal)

        # ── 提醒记录表格
        lbl_hist = QLabel("提醒记录")
        lbl_hist.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px; font-weight: 600;")
        root.addWidget(lbl_hist)

        self.alert_table = QTableWidget()
        self.alert_table.setColumnCount(4)
        self.alert_table.setHorizontalHeaderLabels(["时间", "股票", "信号", "触发价"])
        self.alert_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.alert_table.verticalHeader().setVisible(False)
        self.alert_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.alert_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.alert_table.setMaximumHeight(160)
        root.addWidget(self.alert_table, stretch=1)

    # ── 外部接口 ─────────────────────────────

    def set_stocks(self, codes: list[str], stock_list: list[dict] = None):
        """主窗口调用：同步报价栏股票列表到下拉框，stock_list 用于显示名称"""
        name_map = {s["code"]: s["name"] for s in stock_list} if stock_list else {}
        current_code = self.combo_code.currentData() or ""
        self.combo_code.blockSignals(True)
        self.combo_code.clear()
        for c in codes:
            name = name_map.get(c, "")
            label = f"{name} ({c})" if name else c
            self.combo_code.addItem(label, c)   # userData = 纯代码
        # 恢复之前选中的股票
        if current_code:
            for i in range(self.combo_code.count()):
                if self.combo_code.itemData(i) == current_code:
                    self.combo_code.setCurrentIndex(i)
                    break
        self.combo_code.blockSignals(False)

        # 更新全股后台扫描列表并确保计时器运行
        self._all_codes = list(codes)
        if self._all_codes:
            if not self._all_timer.isActive():
                self._all_timer.start(60_000)   # 每 60 秒扫一轮
        else:
            self._all_timer.stop()

    # ── 控制 ─────────────────────────────────

    def _on_code_changed(self):
        """切换股票时清空旧数据，立即拉取并自动启动定时刷新。"""
        self._data = []
        self._current_code = ""   # 强制触发 is_new 逻辑
        self._signal_points = []
        self.alert_table.setRowCount(0)
        self._ax_price.clear()
        self._ax_pct.clear()
        self._ax_macd.clear()
        self._canvas.draw()
        self.lbl_signal.setText("加载中…")
        self._fetch()
        # 自动启动/重置定时刷新
        if self.combo_code.currentData():
            self._timer.start(self.spin_interval.value() * 1000)
        else:
            self._timer.stop()

    def _on_interval_changed(self, value: int):
        """刷新间隔改变时，若计时器正在运行则重置。"""
        if self._timer.isActive():
            self._timer.start(value * 1000)
        self.lbl_signal.setText("")

    # ── 数据获取 ─────────────────────────────

    def _fetch(self):
        code = (self.combo_code.currentData() or "").strip()
        if not code:
            return
        t = IntradayFetchThread(code)
        t.data_ready.connect(self._on_data_ready)
        t.fetch_error.connect(lambda e: self.lbl_signal.setText(f"获取失败: {e}"))
        t.setParent(self)
        t.finished.connect(t.deleteLater)
        t.start()

    def _on_data_ready(self, items: list, pre_price: float):
        if not items:
            self.lbl_signal.setText("暂无分时数据（非交易时段）")
            return

        code    = self.combo_code.currentData() or ""
        is_new  = (code != self._current_code)

        if is_new:
            self._current_code = code
            self._signal_points = []
            self.alert_table.setRowCount(0)

        prev_len       = len(self._data)
        self._data     = items
        self._pre_price = pre_price

        if is_new:
            # 首次加载：扫描全部历史数据，不 beep
            self._scan_signals(start=5, end=len(items), beep=False)
        else:
            # 增量：只扫新进来的分钟，发现信号则 beep
            self._scan_signals(start=max(5, prev_len), end=len(items), beep=True)

        self._draw_chart()
        # 状态行：显示最新 VWAP 偏离（未触发信号时）
        self._update_vwap_label()

    # ── 绘图 ─────────────────────────────────

    # 全天交易分钟数：09:30-11:30(120) + 13:00-15:00(120) = 240
    _TOTAL_MINS = 240
    # 固定刻度：(坐标位置, 显示标签)
    _AXIS_TICKS = [
        (0,   "09:30"), (30,  "10:00"), (60,  "10:30"), (90,  "11:00"),
        (120, "13:00"), (150, "13:30"), (180, "14:00"), (210, "14:30"),
        (240, "15:00"),
    ]

    def _draw_chart(self):
        import matplotlib.ticker as mticker

        items = self._data
        if not items:
            return

        vwap_all = _build_vwap(items)
        xs, prices, avg_prices = [], [], []
        for d, vw in zip(items, vwap_all):
            pos = _time_to_pos(d["time"])
            if pos is None:
                continue
            xs.append(pos)
            prices.append(d["price"])
            avg_prices.append(vw)

        if not xs:
            return

        ax_p = self._ax_price
        ax_r = self._ax_pct
        ax_m = self._ax_macd
        ax_p.clear()
        ax_r.clear()
        ax_r.set_visible(True)   # clear() 不会重置 visibility，需手动恢复
        ax_m.clear()

        # ── 价格线 + 分时均线
        line_color = COLOR_RED if prices[-1] >= (self._pre_price or prices[0]) else COLOR_GREEN
        ax_p.plot(xs, prices,     color=line_color, linewidth=1.2, label="Price")
        ax_p.plot(xs, avg_prices, color="#F59E0B",  linewidth=0.9, linestyle="--", label="均线")

        if self._pre_price:
            ax_p.axhline(self._pre_price, color=COLOR_MUTED,
                         linewidth=0.6, linestyle=":")

        ax_p.set_xlim(0, self._TOTAL_MINS)
        ax_p.legend(loc="upper left", fontsize=9, framealpha=0.3)
        ax_p.set_ylabel("价格", fontsize=9, color=COLOR_MUTED)
        ax_p.grid(True, linewidth=0.4)
        ax_p.tick_params(labelbottom=False, colors=COLOR_MUTED, labelsize=8)
        ax_p.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
        ax_p.axvline(120, color=COLOR_BORDER, linewidth=0.8, linestyle="--")

        # ── 价格 Y 轴范围（显式设置，防止后续 scatter 引起自动扩展）
        pre = self._pre_price or (prices[0] if prices else 0)
        p_lo = min(min(prices), pre) if pre else min(prices)
        p_hi = max(max(prices), pre) if pre else max(prices)
        pad  = (p_hi - p_lo) * 0.08 or (pre * 0.02 if pre else 0.5)
        ax_p_lo = p_lo - pad
        ax_p_hi = p_hi + pad
        ax_p.set_ylim(ax_p_lo, ax_p_hi)   # 固定后 scatter 不会再扩展

        # ── 右 Y 轴：涨跌幅（与左轴同像素范围，保证对齐）
        if pre:
            ax_r.set_ylim(
                (ax_p_lo - pre) / pre * 100,
                (ax_p_hi - pre) / pre * 100,
            )
            ax_r.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:+.1f}%")
            )
            ax_r.tick_params(axis="y", labelsize=8)
            ax_r.set_navigate(False)
            # 刻度颜色：正数红，负数绿
            for lbl in ax_r.get_yticklabels():
                try:
                    val = float(lbl.get_text().replace("%", "").replace("+", ""))
                    lbl.set_color(COLOR_RED if val >= 0 else COLOR_GREEN)
                except ValueError:
                    lbl.set_color(COLOR_MUTED)
        else:
            ax_r.set_visible(False)

        # ── B / S 标注
        buy_pts  = [s for s in self._signal_points if s["type"] == "buy"]
        sell_pts = [s for s in self._signal_points if s["type"] == "sell"]
        if buy_pts:
            bx = [s["pos"] for s in buy_pts]
            by = [s["price"] for s in buy_pts]
            ax_p.scatter(bx, by, marker="^", color=COLOR_RED,
                         s=70, zorder=6, linewidths=0)
            for x, y in zip(bx, by):
                ax_p.annotate("B", (x, y), xytext=(0, -14),
                              textcoords="offset points", ha="center",
                              fontsize=8, color=COLOR_RED, fontweight="bold")
        if sell_pts:
            sx = [s["pos"] for s in sell_pts]
            sy = [s["price"] for s in sell_pts]
            ax_p.scatter(sx, sy, marker="v", color=COLOR_GREEN,
                         s=70, zorder=6, linewidths=0)
            for x, y in zip(sx, sy):
                ax_p.annotate("S", (x, y), xytext=(0, 7),
                              textcoords="offset points", ha="center",
                              fontsize=8, color=COLOR_GREEN, fontweight="bold")

        # ── MACD(6,13,5) 面板（prices/xs 已同步过滤，等长）
        dif, dea, hist = _calc_macd(prices)
        hist_colors = [COLOR_RED if h >= 0 else COLOR_GREEN for h in hist]
        ax_m.plot(xs, dif,  color="#F59E0B", linewidth=0.9, label="DIF")
        ax_m.plot(xs, dea,  color="#60A5FA", linewidth=0.9, label="DEA")
        ax_m.bar(xs, hist, color=hist_colors, width=1.0, alpha=0.7)
        ax_m.axhline(0, color=COLOR_BORDER, linewidth=0.6)
        ax_m.axvline(120, color=COLOR_BORDER, linewidth=0.8, linestyle="--")
        ax_m.legend(loc="upper left", fontsize=8, framealpha=0.3)
        ax_m.set_xlim(0, self._TOTAL_MINS)
        ax_m.set_ylabel("MACD", fontsize=9, color=COLOR_MUTED)
        ax_m.grid(True, axis="y", linewidth=0.4)

        tick_pos  = [t[0] for t in self._AXIS_TICKS]
        tick_lbls = [t[1] for t in self._AXIS_TICKS]
        ax_m.set_xticks(tick_pos)
        ax_m.set_xticklabels(tick_lbls, fontsize=8, color=COLOR_TEXT)
        ax_m.tick_params(axis="x", colors=COLOR_TEXT, labelsize=8, length=4)
        ax_m.tick_params(axis="y", colors=COLOR_MUTED, labelsize=8)

        self._canvas.draw()

    # ── 信号扫描（方案五组合信号）────────────

    # 同类信号最小间隔（数据点数，1点≈1分钟）
    _MIN_GAP = 5

    def _apply_threshold(self):
        """重新扫描全天 MACD 信号并刷新图表。"""
        if not self._data:
            return
        self._signal_points = []
        self.alert_table.setRowCount(0)
        self._scan_signals(start=0, end=len(self._data), beep=False)
        self._draw_chart()
        self._update_vwap_label()

    def _scan_signals(self, start: int, end: int, beep: bool):
        """MACD(6,13,5) + 均线 + 零轴过滤做T信号。"""
        items = self._data
        n     = len(items)

        prices = [d["price"] for d in items]
        dif, dea, _ = _calc_macd(prices)
        vwap = _build_vwap(items)

        last_buy_idx  = -self._MIN_GAP
        last_sell_idx = -self._MIN_GAP
        for sp in self._signal_points:
            if sp["type"] == "buy":
                last_buy_idx  = max(last_buy_idx,  sp["idx"])
            else:
                last_sell_idx = max(last_sell_idx, sp["idx"])

        code = self.combo_code.currentData() or ""

        scan_start = max(start, 14)
        for i in range(scan_start, end):
            d         = items[i]
            cur_price = d["price"]
            avg_price = vwap[i]

            golden = dif[i-1] <= dea[i-1] and dif[i] > dea[i]
            death  = dif[i-1] >= dea[i-1] and dif[i] < dea[i]

            pos = _time_to_pos(d["time"])
            if pos is None:
                continue

            # 买：金叉 + DEA在零轴下 + 价格在均线下
            if golden and dea[i] < 0 and cur_price < avg_price:
                if i - last_buy_idx >= self._MIN_GAP:
                    last_buy_idx = i
                    sp = {"type": "buy", "pos": pos, "price": cur_price,
                          "time": d["time"], "idx": i}
                    self._signal_points.append(sp)
                    self._add_alert_row(sp, code, beep=(beep and i == end - 1))

            # 卖：死叉 + DIF在零轴上 + 价格在均线上
            elif death and dif[i] > 0 and cur_price > avg_price:
                if i - last_sell_idx >= self._MIN_GAP:
                    last_sell_idx = i
                    sp = {"type": "sell", "pos": pos, "price": cur_price,
                          "time": d["time"], "idx": i}
                    self._signal_points.append(sp)
                    self._add_alert_row(sp, code, beep=(beep and i == end - 1))

    def _add_alert_row(self, sp: dict, code: str, beep: bool = False):
        is_buy = sp["type"] == "buy"
        signal = "▼ 买入" if is_buy else "▲ 卖出"
        color  = COLOR_RED if is_buy else COLOR_GREEN
        bg     = "#FDECEA" if is_buy else "#E8F8ED"

        # 提醒记录表格（最新在最上方）
        self.alert_table.insertRow(0)
        for col, val in enumerate([sp["time"], code, signal, f"¥{sp['price']:.2f}"]):
            item = QTableWidgetItem(val)
            item.setForeground(QColor(color))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.alert_table.setItem(0, col, item)

        if beep:
            self.lbl_signal.setText(
                f"{signal}  {code}  ¥{sp['price']:.2f}  @ {sp['time']}"
            )
            self.lbl_signal.setStyleSheet(
                f"font-size: 14px; font-weight: 700; color: {color};"
                f"background-color: {bg}; padding: 6px; border-radius: 4px;"
            )
            self.signal_triggered.emit(code, sp["type"])

    def _update_vwap_label(self):
        items = self._data
        if not items:
            return
        prices = [d["price"] for d in items]
        dif, dea, _ = _calc_macd(prices)
        cur = items[-1]["price"]
        last_dif = dif[-1]
        last_dea = dea[-1]
        diff = last_dif - last_dea
        sign = "+" if diff >= 0 else ""
        state = "金叉" if last_dif >= last_dea else "死叉"
        last_sig = self._signal_points[-1] if self._signal_points else None
        if last_sig and last_sig["idx"] == len(items) - 1:
            return
        self.lbl_signal.setText(
            f"当前 {cur:.2f}  |  DIF {last_dif:.4f}  DEA {last_dea:.4f}  |  {state} {sign}{diff:.4f}"
        )
        self.lbl_signal.setStyleSheet(
            f"font-size: 13px; color: {COLOR_MUTED}; padding: 4px;"
        )

    # ── 全股票后台扫描 ────────────────────────

    def _run_all_scan(self):
        """启动一轮多股扫描（若上一轮未完成则跳过）。"""
        if self._all_scanner and self._all_scanner.isRunning():
            return
        # 排除当前详细图正在监控的股票（避免重复提醒）
        skip = self._current_code
        codes = [c for c in self._all_codes if c != skip]
        if not codes:
            return
        t = MultiStockScanner(codes, self._all_state, parent=self)
        t.signal_found.connect(self._on_multi_signal)
        t.scan_done.connect(self._on_multi_scan_done)
        t.finished.connect(t.deleteLater)
        self._all_scanner = t
        t.start()

    def _on_multi_signal(self, code: str, sig_type: str, price: float, time_str: str):
        """多股扫描发现信号 → 加入提醒表格 + 胶囊 badge + beep。"""
        is_buy = (sig_type == "buy")
        signal = "▼ 买入" if is_buy else "▲ 卖出"
        color  = COLOR_RED if is_buy else COLOR_GREEN
        bg     = "#FDECEA" if is_buy else "#E8F8ED"

        self.alert_table.insertRow(0)
        for col, val in enumerate([time_str, code, signal, f"¥{price:.2f}"]):
            item = QTableWidgetItem(val)
            item.setForeground(QColor(color))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.alert_table.setItem(0, col, item)

        self.lbl_signal.setText(f"{signal}  {code}  ¥{price:.2f}  @ {time_str}")
        self.lbl_signal.setStyleSheet(
            f"font-size: 14px; font-weight: 700; color: {color};"
            f"background-color: {bg}; padding: 6px; border-radius: 4px;"
        )
        QApplication.beep()
        self.signal_triggered.emit(code, sig_type)

    def _on_multi_scan_done(self, new_state: object):
        """更新多股扫描状态，释放扫描线程引用。"""
        self._all_state.update(new_state)
        self._all_scanner = None


# ──────────────────────────────────────────
# 样式表
# ──────────────────────────────────────────

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: "-apple-system", "SF Pro Text", "Helvetica Neue", Arial;
    font-size: 13px;
}}
QGroupBox {{
    background-color: transparent;
    border: none;
    margin-top: 24px;
    padding: 0;
    font-weight: 600;
    color: {COLOR_MUTED};
    font-size: 11px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 0px;
    padding: 0;
    color: {COLOR_MUTED};
    font-size: 11px;
}}
QLabel {{
    color: {COLOR_TEXT};
}}
QSpinBox, QLineEdit {{
    background-color: {COLOR_INPUT_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_SEL};
}}
QSpinBox:focus, QLineEdit:focus {{
    border: 1.5px solid {COLOR_ACCENT};
    background-color: {COLOR_INPUT_BG};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0;
    height: 0;
    border: none;
    background: transparent;
    image: none;
}}
QPushButton {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 7px 14px;
    font-weight: 400;
    color: {COLOR_TEXT};
    font-size: 13px;
    background-color: transparent;
}}
QPushButton:hover {{
    color: {COLOR_TEXT};
    background-color: {COLOR_SURFACE};
}}
QPushButton#btn_start {{
    background-color: {COLOR_ACCENT};
    color: {COLOR_TEXT};
    font-weight: 600;
    border-color: {COLOR_ACCENT};
}}
QPushButton#btn_start:hover {{
    background-color: #1A8AFF;
    color: {COLOR_TEXT};
    border-color: #1A8AFF;
}}
QPushButton#btn_start:disabled {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_MUTED};
}}
QPushButton#btn_stop {{
    background-color: transparent;
    color: {COLOR_TEXT};
}}
QPushButton#btn_stop:hover {{
    background-color: {COLOR_SURFACE};
}}
QPushButton#btn_stop:disabled {{
    color: {COLOR_BORDER};
}}
QPushButton#btn_once {{
    background-color: transparent;
    color: {COLOR_TEXT};
}}
QPushButton#btn_once:hover {{
    background-color: {COLOR_SURFACE};
}}
QPushButton#btn_once:disabled {{
    color: {COLOR_BORDER};
}}
QPushButton#btn_excel {{
    background-color: transparent;
    color: {COLOR_TEXT};
}}
QPushButton#btn_excel:hover {{
    background-color: {COLOR_SURFACE};
}}
QPushButton#btn_browse {{
    background-color: transparent;
    color: {COLOR_TEXT};
    padding: 6px 8px;
    font-size: 12px;
    font-weight: 400;
    border-radius: 4px;
}}
QPushButton#btn_browse:hover {{
    color: {COLOR_TEXT};
    background-color: {COLOR_SURFACE};
}}
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {COLOR_BORDER};
    background-color: transparent;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {COLOR_MUTED};
    border: none;
    padding: 10px 24px;
    font-size: 13px;
    font-weight: 400;
}}
QTabBar::tab:selected {{
    color: {COLOR_TEXT};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {COLOR_TEXT};
}}
QTabBar::tab:disabled {{
    color: {COLOR_BORDER};
}}
QTextEdit {{
    background-color: {COLOR_BG};
    border: none;
    color: {COLOR_TEXT};
    font-family: "SF Mono", "Menlo", "Monaco", monospace;
    font-size: 12px;
    padding: 12px;
}}
QTableWidget {{
    background-color: {COLOR_BG};
    border: none;
    gridline-color: transparent;
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_SEL};
    outline: 0;
}}
QTableWidget::item {{
    padding: 8px 10px;
    border-bottom: 1px solid {COLOR_ROW_SEP};
}}
QTableWidget::item:selected {{
    background-color: {COLOR_SEL};
    color: {COLOR_TEXT};
}}
QHeaderView::section {{
    background-color: {COLOR_BG};
    color: {COLOR_MUTED};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 8px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QScrollBar:vertical {{
    background-color: transparent;
    width: 4px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background-color: {COLOR_BORDER};
    border-radius: 2px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {COLOR_MUTED};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background-color: transparent;
    height: 4px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background-color: {COLOR_BORDER};
    border-radius: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {COLOR_MUTED};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QCheckBox {{
    color: {COLOR_TEXT};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1.5px solid {COLOR_BORDER};
    border-radius: 3px;
    background-color: transparent;
}}
QCheckBox::indicator:checked {{
    background-color: {COLOR_ACCENT};
    border-color: {COLOR_ACCENT};
}}
QSplitter::handle {{
    background-color: transparent;
    width: 0px;
}}
QStatusBar {{
    background-color: {COLOR_BG};
    color: {COLOR_MUTED};
    border: none;
    font-size: 12px;
    padding: 0 16px;
}}
QListWidget {{
    background-color: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    color: {COLOR_TEXT};
    font-size: 13px;
    outline: none;
}}
QListWidget::item:selected {{
    background-color: {COLOR_SEL};
    color: {COLOR_TEXT};
}}
QListWidget::item:hover {{
    background-color: {COLOR_SURFACE};
}}
"""


# ──────────────────────────────────────────
# iOS 风格 Toggle Switch
# ──────────────────────────────────────────

from PyQt6.QtWidgets import QCheckBox as _QCheckBox

class _ToggleSwitch(_QCheckBox):
    """iOS 风格拨动开关，完全自绘，替代 QCheckBox。"""
    _TW = 34    # 轨道宽
    _TH = 18    # 轨道高
    _C_ON  = "#34C759"   # iOS 绿（选中）
    _C_OFF = "#E5E5EA"   # 浅灰（未选中）

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font())
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        w = self._TW + (10 + text_w if text_w else 0)
        return QSize(w, max(self._TH, fm.height()))

    def minimumSizeHint(self):
        return self.sizeHint()

    def hitButton(self, pos):
        return self.contentsRect().contains(pos)

    def paintEvent(self, _event):
        from PyQt6.QtGui import QPainter, QBrush, QColor
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        ty = (self.height() - self._TH) // 2

        # 轨道（圆角矩形）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(self._C_ON if self.isChecked() else self._C_OFF)))
        p.drawRoundedRect(0, ty, self._TW, self._TH, self._TH / 2, self._TH / 2)

        # 圆形旋钮
        m = 2
        kd = self._TH - 2 * m
        kx = self._TW - m - kd if self.isChecked() else m
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(kx, ty + m, kd, kd)

        # 文字标签
        if self.text():
            p.setPen(QColor(COLOR_TEXT))
            p.setFont(self.font())
            fm = p.fontMetrics()
            tx = self._TW + 10
            ty_text = (self.height() + fm.ascent() - fm.descent()) // 2
            p.drawText(tx, ty_text, self.text())

        p.end()


# ──────────────────────────────────────────
# AISettingsDialog — AI API 配置对话框
# ──────────────────────────────────────────

class AISettingsDialog(QDialog):
    """AI 提供商配置对话框，支持 Claude CLI / Claude API / ChatGPT / Gemini / KIMI / QWen / 自定义"""

    PROVIDERS = [
        ("kiro_cli",   "Kiro CLI（本地命令行）"),
        ("claude_cli", "Claude CLI（本地命令行）"),
        ("claude_api", "Claude API（Anthropic）"),
        ("openai",     "ChatGPT / OpenAI"),
        ("gemini",     "Gemini（Google）"),
        ("kimi",       "KIMI（月之暗面）"),
        ("qwen",       "通义千问（阿里云）"),
        ("custom",     "自定义（OpenAI 兼容）"),
    ]

    # 各提供商常用模型列表（用于提示）
    PROVIDER_MODELS = {
        "claude_api": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022"],
        "openai":     ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"],
        "gemini":     ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"],
        "kimi":       ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "qwen":       ["qwen-turbo", "qwen-plus", "qwen-max"],
        "custom":     [],
    }

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = dict(config)
        self._test_thread = None
        self.setWindowTitle("AI API 设置")
        self.setMinimumWidth(440)
        self.setModal(True)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {COLOR_BG};
            }}
            QLabel {{
                color: {COLOR_TEXT};
            }}
            QComboBox {{
                background-color: {COLOR_INPUT_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                padding: 6px 10px;
                color: {COLOR_TEXT};
                font-size: 13px;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                color: {COLOR_TEXT};
                selection-background-color: {COLOR_SEL};
            }}
            QLineEdit {{
                background-color: {COLOR_INPUT_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                padding: 6px 10px;
                color: {COLOR_TEXT};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border: 1.5px solid {COLOR_ACCENT};
            }}
            QPushButton {{
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                padding: 7px 14px;
                color: {COLOR_TEXT};
                font-size: 13px;
                background-color: transparent;
            }}
            QPushButton:hover {{
                background-color: {COLOR_SURFACE};
            }}
            QPushButton#btn_save_ai {{
                background-color: {COLOR_ACCENT};
                color: white;
                border-color: {COLOR_ACCENT};
                font-weight: 600;
            }}
            QPushButton#btn_save_ai:hover {{
                background-color: #1A8AFF;
                border-color: #1A8AFF;
            }}
        """)
        self._build_ui()
        self._load_from_config()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 20, 24, 20)

        # ── 标题
        title_lbl = QLabel("AI API 设置")
        title_lbl.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {COLOR_TEXT};")
        layout.addWidget(title_lbl)

        # ── 提供商选择
        self._add_label(layout, "AI 提供商")
        self.combo_provider = QComboBox()
        self.combo_provider.setFixedHeight(34)
        for key, label in self.PROVIDERS:
            self.combo_provider.addItem(label, key)
        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        layout.addWidget(self.combo_provider)

        # ── Claude CLI 路径（仅 claude_cli 显示）
        self.row_claude_bin = self._make_row()
        self._add_label(self.row_claude_bin.layout(), "Claude 可执行路径")
        self.edit_claude_bin = QLineEdit()
        self.edit_claude_bin.setFixedHeight(34)
        self.edit_claude_bin.setPlaceholderText("例：/usr/local/bin/claude")
        self.row_claude_bin.layout().addWidget(self.edit_claude_bin)
        layout.addWidget(self.row_claude_bin)

        # ── API Key（非 claude_cli 显示）
        self.row_api_key = self._make_row()
        self.lbl_api_key = QLabel("API Key")
        self.lbl_api_key.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px; font-weight: 600;")
        self.row_api_key.layout().addWidget(self.lbl_api_key)
        self.edit_api_key = QLineEdit()
        self.edit_api_key.setFixedHeight(34)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("sk-...")
        self.row_api_key.layout().addWidget(self.edit_api_key)
        layout.addWidget(self.row_api_key)

        # ── 模型（非 claude_cli 显示）
        self.row_model = self._make_row()
        self._add_label(self.row_model.layout(), "模型")
        self.edit_model = QLineEdit()
        self.edit_model.setFixedHeight(34)
        self.edit_model.setPlaceholderText("留空使用默认模型")
        self.row_model.layout().addWidget(self.edit_model)
        self.lbl_model_hint = QLabel("")
        self.lbl_model_hint.setStyleSheet(
            f"color: {COLOR_MUTED}; font-size: 10px;"
        )
        self.lbl_model_hint.setWordWrap(True)
        self.row_model.layout().addWidget(self.lbl_model_hint)
        layout.addWidget(self.row_model)

        # ── Base URL（custom / claude_api 显示）
        self.row_base_url = self._make_row()
        self._add_label(self.row_base_url.layout(), "Base URL")
        self.edit_base_url = QLineEdit()
        self.edit_base_url.setFixedHeight(34)
        self.edit_base_url.setPlaceholderText("https://api.example.com/v1")
        self.row_base_url.layout().addWidget(self.edit_base_url)
        layout.addWidget(self.row_base_url)

        # ── 按钮行
        layout.addSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_test = QPushButton("测试连接")
        self.btn_test.setFixedHeight(34)
        self.btn_test.clicked.connect(self._test_connection)

        btn_cancel = QPushButton("取消")
        btn_cancel.setFixedHeight(34)
        btn_cancel.clicked.connect(self.reject)

        btn_save = QPushButton("保存")
        btn_save.setObjectName("btn_save_ai")
        btn_save.setFixedHeight(34)
        btn_save.clicked.connect(self._save_and_close)

        btn_row.addWidget(self.btn_test)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

    def _make_row(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)
        return w

    def _add_label(self, layout, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px; font-weight: 600;")
        layout.addWidget(lbl)

    def _load_from_config(self):
        provider = self._config.get("ai_provider", "claude_cli")
        idx = next((i for i, (k, _) in enumerate(self.PROVIDERS) if k == provider), 0)
        self.combo_provider.setCurrentIndex(idx)
        self.edit_claude_bin.setText(
            self._config.get("claude_bin", "") or ConfigManager.detect_claude_bin()
        )
        self.edit_api_key.setText(self._config.get("ai_api_key", ""))
        defaults = _AI_PROVIDER_DEFAULTS.get(provider, {})
        self.edit_model.setText(self._config.get("ai_model", "") or defaults.get("model", ""))
        self.edit_base_url.setText(self._config.get("ai_base_url", "") or defaults.get("base_url", ""))
        self._update_visibility(provider)

    def _on_provider_changed(self, _idx: int):
        provider = self.combo_provider.currentData()
        defaults = _AI_PROVIDER_DEFAULTS.get(provider, {})
        # 只在字段为空时自动填充默认值
        if not self.edit_model.text():
            self.edit_model.setText(defaults.get("model", ""))
        if not self.edit_base_url.text():
            self.edit_base_url.setText(defaults.get("base_url", ""))
        self._update_visibility(provider)
        self.adjustSize()

    def _update_visibility(self, provider: str):
        is_cli = (provider in ("claude_cli", "kiro_cli"))
        show_base_url = provider in ("claude_api", "custom")
        self.row_claude_bin.setVisible(provider == "claude_cli")
        self.row_api_key.setVisible(not is_cli)
        self.row_model.setVisible(not is_cli)
        self.row_base_url.setVisible(show_base_url)
        # 更新模型提示
        models = self.PROVIDER_MODELS.get(provider, [])
        if models:
            self.lbl_model_hint.setText("可用模型：" + "  |  ".join(models))
        else:
            self.lbl_model_hint.setText("")

    def _build_test_config(self) -> dict:
        cfg = dict(self._config)
        cfg["ai_provider"] = self.combo_provider.currentData()
        cfg["claude_bin"]  = self.edit_claude_bin.text().strip()
        cfg["ai_api_key"]  = self.edit_api_key.text().strip()
        cfg["ai_model"]    = self.edit_model.text().strip()
        cfg["ai_base_url"] = self.edit_base_url.text().strip()
        return cfg

    def _test_connection(self):
        from PyQt6.QtWidgets import QMessageBox
        import requests as _req
        provider = self.combo_provider.currentData()
        if provider == "claude_cli":
            QMessageBox.information(self, "提示", "Claude CLI 无需测试连接，保存后直接使用。")
            return

        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中...")
        cfg = self._build_test_config()

        class _TestThread(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, c):
                super().__init__()
                self._c = c
            def run(self):
                import requests as _rq
                try:
                    prov     = self._c.get("ai_provider", "openai")
                    api_key  = self._c.get("ai_api_key", "")
                    model    = self._c.get("ai_model", "") or _AI_PROVIDER_DEFAULTS.get(prov, {}).get("model", "")
                    base_url = self._c.get("ai_base_url", "") or _AI_PROVIDER_DEFAULTS.get(prov, {}).get("base_url", "")
                    test_msg = "请回复OK"

                    if prov == "gemini":
                        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                               f"{model or 'gemini-1.5-flash'}:generateContent?key={api_key}")
                        data = {"contents": [{"parts": [{"text": test_msg}]}],
                                "generationConfig": {"maxOutputTokens": 20}}
                        r = _rq.post(url, json=data, timeout=30)
                        r.raise_for_status()
                        content = r.json()["candidates"][0]["content"]["parts"][0]["text"]

                    elif prov == "claude_api":
                        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                   "Content-Type": "application/json"}
                        data = {"model": model or "claude-3-5-sonnet-20241022",
                                "max_tokens": 20,
                                "messages": [{"role": "user", "content": test_msg}]}
                        url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
                        r = _rq.post(url, headers=headers, json=data, timeout=30)
                        r.raise_for_status()
                        content = r.json()["content"][0]["text"]

                    else:
                        headers = {"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"}
                        data = {"messages": [{"role": "user", "content": test_msg}],
                                "max_tokens": 20}
                        if model:
                            data["model"] = model
                        url = base_url.rstrip("/") + "/chat/completions"
                        r = _rq.post(url, headers=headers, json=data, timeout=30)
                        r.raise_for_status()
                        content = r.json()["choices"][0]["message"]["content"]

                    self.done.emit(True, f"连接成功！\nAI 回复：{content.strip()[:120]}")
                except Exception as e:
                    msg = str(e)
                    if "429" in msg:
                        self.done.emit(False, "请求频率超限（429 Too Many Requests）。\nAPI Key 和模型名称正确，稍等 1-2 分钟后重试即可。")
                    elif "401" in msg or "403" in msg:
                        self.done.emit(False, f"认证失败（{msg[:80]}）。\n请检查 API Key 是否正确。")
                    else:
                        self.done.emit(False, f"连接失败：{msg}")

        def on_done(ok: bool, msg: str):
            self.btn_test.setEnabled(True)
            self.btn_test.setText("测试连接")
            if ok:
                QMessageBox.information(self, "测试结果", msg)
            else:
                QMessageBox.warning(self, "测试失败", msg)

        self._test_thread = _TestThread(cfg)
        self._test_thread.done.connect(on_done)
        self._test_thread.start()

    def _save_and_close(self):
        self._config = self._build_test_config()
        self.accept()

    def get_config(self) -> dict:
        return self._config


# ──────────────────────────────────────────
# DesktopWidget — 桌面浮动小组件
# ──────────────────────────────────────────

# ── 自绘图标按钮（macOS 风格，无 emoji）──

class _IconButton(QPushButton):
    """通用自绘小图标按钮基类，flat + 透明背景，子类覆写 _draw_icon。"""

    def __init__(self, size: int = 22, parent=None):
        super().__init__(parent)
        self._icon_size = size
        self.setFixedSize(size, size)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background: transparent; border: none;")

    def paintEvent(self, _event):
        from PyQt6.QtGui import QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_icon(p, self._icon_size)
        p.end()

    def _draw_icon(self, p, s: int):
        pass  # 子类实现


class _PinButton(_IconButton):
    """
    置顶切换按钮。
    图标形态：圆形针头 + 垂直针杆
      active  ：主色蓝实心圆 + 蓝色实线杆
      inactive：灰色空心圆 + 灰色细线杆（轻微倾斜表示"松开"）
    """

    def __init__(self, parent=None):
        super().__init__(22, parent)
        self._active = True
        self._update_tooltip()

    def set_active(self, active: bool):
        self._active = active
        self._update_tooltip()
        self.update()

    def _update_tooltip(self):
        self.setToolTip("已置顶 — 点击取消固定" if self._active else "未置顶 — 点击固定到最前")

    def _draw_icon(self, p, s: int):
        from PyQt6.QtGui import QPen, QBrush, QColor
        cx = s / 2

        if self._active:
            pin_color = QColor(255, 255, 255, 210)  # 高亮白（激活）
        else:
            pin_color = QColor(255, 255, 255, 90)   # 淡白（非激活）

        # ── 针头：圆形
        r_head = s * 0.21
        head_cx, head_cy = cx, s * 0.30
        p.setPen(QPen(pin_color, 1.4))
        p.setBrush(QBrush(pin_color) if self._active else QBrush(Qt.GlobalColor.transparent))
        p.drawEllipse(
            int(head_cx - r_head), int(head_cy - r_head),
            int(r_head * 2), int(r_head * 2),
        )

        # ── 针杆：active=垂直, inactive=轻微倾斜（视觉区分"松开"）
        pen_w = 1.6 if self._active else 1.2
        p.setPen(QPen(pin_color, pen_w,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        shaft_top = int(head_cy + r_head)
        shaft_bot = s - 3
        if self._active:
            p.drawLine(int(cx), shaft_top, int(cx), shaft_bot)
        else:
            p.drawLine(int(cx) - 1, shaft_top, int(cx) + 1, shaft_bot)

        # ── 针尖：小实心圆
        tip_r = 1.8 if self._active else 1.4
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(pin_color))
        p.drawEllipse(
            int(cx - tip_r), int(shaft_bot - tip_r),
            int(tip_r * 2), int(tip_r * 2),
        )


class _CloseButton(_IconButton):
    """
    关闭/隐藏按钮。
    图标：两条交叉对角线（×），hover 变深色。
    """

    def __init__(self, parent=None):
        super().__init__(22, parent)
        self._hovered = False
        self.setToolTip("隐藏小组件")

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def _draw_icon(self, p, s: int):
        from PyQt6.QtGui import QPen, QColor
        color = QColor(255, 255, 255, 210) if self._hovered else QColor(255, 255, 255, 90)
        m = s * 0.28
        p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(int(m), int(m), int(s - m), int(s - m))
        p.drawLine(int(s - m), int(m), int(m), int(s - m))


class _AddButton(QPushButton):
    """
    蓝底白色加号圆角正方形按钮，用 QPainter 绘制保证 + 完全居中。
    """
    _SIZE   = 20
    _RADIUS = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pressed = False
        self._hovered = False

    def enterEvent(self, e):
        self._hovered = True;  self.update();  super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False; self.update();  super().leaveEvent(e)

    def mousePressEvent(self, e):
        self._pressed = True;  self.update();  super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._pressed = False; self.update();  super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        from PyQt6.QtGui import QPainter, QBrush, QColor, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._SIZE

        # 背景色
        if self._pressed:
            bg = QColor("#0055BB")
        elif self._hovered:
            bg = QColor("#0066DD")
        else:
            bg = QColor(COLOR_ACCENT)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(0, 0, s, s, self._RADIUS, self._RADIUS)

        # 白色加号，居中
        arm   = s * 0.18        # 横/竖臂半长
        cx    = s / 2
        cy    = s / 2
        thick = 2.0
        from PyQt6.QtCore import QPointF
        pen = QPen(QColor("#FFFFFF"), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
        p.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))
        p.end()


def _native_window_drag(widget: "QWidget") -> bool:
    """
    调用 macOS 原生 performWindowDragWithEvent: 开始拖动窗口。
    必须在 mousePressEvent 中同步调用（此时 currentEvent 仍是 mouseDown 事件）。
    """
    import sys
    if sys.platform != "darwin":
        return False
    try:
        import ctypes, ctypes.util
        libobjc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        libobjc.sel_registerName.restype  = ctypes.c_void_p
        libobjc.objc_getClass.restype     = ctypes.c_void_p
        libobjc.objc_msgSend.restype      = ctypes.c_void_p
        libobjc.objc_msgSend.argtypes     = [ctypes.c_void_p, ctypes.c_void_p]

        def sel(name):
            return ctypes.c_void_p(libobjc.sel_registerName(name.encode()))

        def msg(obj, s, *args):
            libobjc.objc_msgSend.restype  = ctypes.c_void_p
            libobjc.objc_msgSend.argtypes = (
                [ctypes.c_void_p, ctypes.c_void_p]
                + [ctypes.c_void_p] * len(args)
            )
            return libobjc.objc_msgSend(obj, s, *args)

        # 获取 NSWindow
        qt_view = ctypes.c_void_p(int(widget.winId()))
        ns_win  = ctypes.c_void_p(msg(qt_view, sel("window")))

        # 获取当前 NSEvent（mouseDown 事件，此刻仍在事件队列顶端）
        ns_app_cls = ctypes.c_void_p(libobjc.objc_getClass(b"NSApplication"))
        ns_app     = ctypes.c_void_p(msg(ns_app_cls, sel("sharedApplication")))
        ns_event   = ctypes.c_void_p(msg(ns_app, sel("currentEvent")))

        # 启动原生窗口拖动（内部运行 modal run loop 直到鼠标释放）
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        libobjc.objc_msgSend(ns_win, sel("performWindowDragWithEvent:"), ns_event)
        return True
    except Exception:
        return False


class _DragHandle(QWidget):
    """
    可拖动标题栏。
    - macOS：调用 performWindowDragWithEvent: 原生拖动（QLabel 不消费事件，
      会向上传播到此处，按钮消费事件故不传播，自然豁免）
    - 其他平台：记录鼠标偏移量，在 mouseMoveEvent 中调用 move()
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        import sys
        if sys.platform == "darwin":
            _native_window_drag(self.window())
            w = self.window()
            if hasattr(w, "_save_position"):
                w._save_position()
        else:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        w = self.window()
        if hasattr(w, "_save_position"):
            w._save_position()


def _make_radar_logo(size: int = 20, ring: bool = False, dock: bool = False) -> "QPixmap":
    """
    生成红色圆角底 + 白色雷达弧线的 QPixmap logo。
    ring=True : topbar 外圈加半透明红色细圆。
    dock=True : Dock 图标模式，图标内缩 ~15%，外圈为纯透明圆角矩形留白。
    """
    from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
    from PyQt6.QtCore import QRectF

    # topbar ring 模式：pixmap 扩大，外加半透明圆
    ring_pad = 3 if ring else 0
    total = size + ring_pad * 2
    pm = QPixmap(total, total)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    if ring:
        rpen = QPen(QColor(232, 50, 28, 55))
        rpen.setWidthF(1.4)
        p.setPen(rpen)
        p.setBrush(Qt.GlobalColor.transparent)
        m = 0.6
        p.drawEllipse(QRectF(m, m, total - m * 2, total - m * 2))

    # dock 模式：图标内缩留透明外圈（约 13% 边距）
    if dock:
        inset = round(size * 0.13)
        icon_size = size - inset * 2
        ox_off = inset
        oy_off = inset
    else:
        icon_size = size
        ox_off = ring_pad
        oy_off = ring_pad

    # 红色圆角背景
    corner_r = max(4, icon_size * 0.18)   # dock 大图时圆角比例更大
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#E8321C")))
    p.drawRoundedRect(QRectF(ox_off, oy_off, icon_size, icon_size), corner_r, corner_r)

    # 白色雷达弧线
    ox = ox_off + icon_size * 0.22
    oy = oy_off + icon_size * 0.82
    pen = QPen(QColor("white"))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    for r in (icon_size * 0.22, icon_size * 0.42, icon_size * 0.62):
        pen.setWidthF(icon_size * 0.07)
        p.setPen(pen)
        p.drawArc(QRectF(ox - r, oy - r, r * 2, r * 2), 0 * 16, 90 * 16)

    # 信号源圆点
    dot_r = icon_size * 0.1
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("white")))
    p.drawEllipse(QRectF(ox - dot_r, oy - dot_r, dot_r * 2, dot_r * 2))

    p.end()
    return pm


def _apply_macos_vibrancy(widget: "QWidget") -> bool:
    """
    通过 ctypes 调用 Objective-C runtime，将 macOS NSVisualEffectView
    注入为底层背景，实现系统原生毛玻璃模糊效果。
    关键修复：正确获取 contentView 的 CGRect 并设置 VEV 初始 frame，
    确保模糊层覆盖完整窗口区域。
    """
    import sys
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        import ctypes.util

        libobjc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        libobjc.objc_getClass.restype    = ctypes.c_void_p
        libobjc.sel_registerName.restype = ctypes.c_void_p

        def cls(name: str):
            return ctypes.c_void_p(libobjc.objc_getClass(name.encode()))

        def sel(name: str):
            return ctypes.c_void_p(libobjc.sel_registerName(name.encode()))

        # 通用 msg，返回 void*
        def msg(obj, selector, *args):
            libobjc.objc_msgSend.restype  = ctypes.c_void_p
            libobjc.objc_msgSend.argtypes = (
                [ctypes.c_void_p, ctypes.c_void_p]
                + [ctypes.c_long if isinstance(a, int) else ctypes.c_void_p for a in args]
            )
            return libobjc.objc_msgSend(obj, selector, *args)

        # CGRect 结构体（用于读取 / 写入 frame）
        class _CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        class _CGSize(ctypes.Structure):
            _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

        class _CGRect(ctypes.Structure):
            _fields_ = [("origin", _CGPoint), ("size", _CGSize)]

        # Qt winId() → NSView* → NSWindow
        qt_view = ctypes.c_void_p(int(widget.winId()))
        ns_win  = ctypes.c_void_p(msg(qt_view, sel("window")))

        # NSWindow 背景设为透明，模糊层才能透出
        clear = ctypes.c_void_p(msg(cls("NSColor"), sel("clearColor")))
        msg(ns_win, sel("setBackgroundColor:"), clear)
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        libobjc.objc_msgSend(ns_win, sel("setOpaque:"), False)

        content_view = ctypes.c_void_p(msg(ns_win, sel("contentView")))

        # 读取 contentView 当前 frame，用于初始化 VEV 尺寸
        libobjc.objc_msgSend.restype  = _CGRect
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cv_frame = libobjc.objc_msgSend(content_view, sel("frame"))
        w = cv_frame.size.width  or widget.width()
        h = cv_frame.size.height or widget.height()

        # 创建并配置 NSVisualEffectView
        VEV = cls("NSVisualEffectView")
        vev = ctypes.c_void_p(msg(msg(VEV, sel("alloc")), sel("init")))

        # 设置初始 frame = contentView 的完整尺寸
        vev_frame = _CGRect(_CGPoint(0.0, 0.0), _CGSize(w, h))
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _CGRect]
        libobjc.objc_msgSend(vev, sel("setFrame:"), vev_frame)

        # Material 21 = NSVisualEffectMaterialUnderWindowBackground
        # 这是 macOS 天气、通知中心等系统组件使用的强模糊材质
        msg(vev, sel("setMaterial:"), 21)
        # BlendingMode 0 = BehindWindow（透出桌面内容）
        msg(vev, sel("setBlendingMode:"), 0)
        # State 1 = Active
        msg(vev, sel("setState:"), 1)
        # AutoresizingMask 18 = NSViewWidthSizable|NSViewHeightSizable
        msg(vev, sel("setAutoresizingMask:"), 18)

        # 插入 contentView 最底层（NSWindowBelow = 0）
        msg(content_view, sel("addSubview:positioned:relativeTo:"),
            vev, ctypes.c_long(0), ctypes.c_void_p(0))

        # ── 防止切换 App 时面板自动隐藏 ──
        # NSPanel 默认 hidesOnDeactivate=YES，切换到其他 App 时会消失
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        libobjc.objc_msgSend(ns_win, sel("setHidesOnDeactivate:"), False)

        # ── 允许通过拖动窗口背景移动窗口 ──
        libobjc.objc_msgSend(ns_win, sel("setMovableByWindowBackground:"), True)

        # ── 提升窗口层级到 NSFloatingWindowLevel(3) ──
        # 确保在所有普通 App 窗口上方持续可见
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        libobjc.objc_msgSend(ns_win, sel("setLevel:"), 3)

        return True
    except Exception:
        return False


def _apply_main_window_vibrancy(widget: "QWidget") -> bool:
    """
    为主窗口应用 macOS NSVisualEffectView 毛玻璃背景。
    与 _apply_macos_vibrancy 相同，但不设置 setHidesOnDeactivate / setLevel，
    避免干扰主窗口的正常层级行为。
    """
    import sys
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        import ctypes.util

        libobjc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        libobjc.objc_getClass.restype    = ctypes.c_void_p
        libobjc.sel_registerName.restype = ctypes.c_void_p

        def cls(name: str):
            return ctypes.c_void_p(libobjc.objc_getClass(name.encode()))

        def sel(name: str):
            return ctypes.c_void_p(libobjc.sel_registerName(name.encode()))

        def msg(obj, selector, *args):
            libobjc.objc_msgSend.restype  = ctypes.c_void_p
            libobjc.objc_msgSend.argtypes = (
                [ctypes.c_void_p, ctypes.c_void_p]
                + [ctypes.c_long if isinstance(a, int) else ctypes.c_void_p for a in args]
            )
            return libobjc.objc_msgSend(obj, selector, *args)

        class _CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        class _CGSize(ctypes.Structure):
            _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

        class _CGRect(ctypes.Structure):
            _fields_ = [("origin", _CGPoint), ("size", _CGSize)]

        qt_view = ctypes.c_void_p(int(widget.winId()))
        ns_win  = ctypes.c_void_p(msg(qt_view, sel("window")))

        clear = ctypes.c_void_p(msg(cls("NSColor"), sel("clearColor")))
        msg(ns_win, sel("setBackgroundColor:"), clear)
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        libobjc.objc_msgSend(ns_win, sel("setOpaque:"), False)

        content_view = ctypes.c_void_p(msg(ns_win, sel("contentView")))

        libobjc.objc_msgSend.restype  = _CGRect
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cv_frame = libobjc.objc_msgSend(content_view, sel("frame"))
        w = cv_frame.size.width  or widget.width()
        h = cv_frame.size.height or widget.height()

        VEV = cls("NSVisualEffectView")
        vev = ctypes.c_void_p(msg(msg(VEV, sel("alloc")), sel("init")))

        vev_frame = _CGRect(_CGPoint(0.0, 0.0), _CGSize(w, h))
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _CGRect]
        libobjc.objc_msgSend(vev, sel("setFrame:"), vev_frame)

        # Material 12 = NSVisualEffectMaterialWindowBackground（主窗口推荐）
        msg(vev, sel("setMaterial:"), 12)
        # BlendingMode 0 = BehindWindow（透出桌面/其他窗口内容）
        msg(vev, sel("setBlendingMode:"), 0)
        # State 1 = Active
        msg(vev, sel("setState:"), 1)
        # AutoresizingMask 18 = NSViewWidthSizable|NSViewHeightSizable
        msg(vev, sel("setAutoresizingMask:"), 18)

        msg(content_view, sel("addSubview:positioned:relativeTo:"),
            vev, ctypes.c_long(0), ctypes.c_void_p(0))

        return True
    except Exception:
        return False


def _fix_widget_float(widget: "QWidget", pinned: bool = True) -> None:
    """
    根据 pinned 状态设置小组件的 macOS 窗口层级和行为。
    pinned=True : NSFloatingWindowLevel(3), 不隐藏, canJoinAllSpaces
    pinned=False: NSNormalWindowLevel(0),   恢复默认行为
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        import ctypes, ctypes.util
        libobjc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        libobjc.sel_registerName.restype = ctypes.c_void_p

        def sel(name: str):
            return ctypes.c_void_p(libobjc.sel_registerName(name.encode()))

        libobjc.objc_msgSend.restype  = ctypes.c_void_p
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        qt_view = ctypes.c_void_p(int(widget.winId()))
        ns_win  = ctypes.c_void_p(libobjc.objc_msgSend(qt_view, sel("window")))

        # setLevel
        level = ctypes.c_long(3 if pinned else 0)
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        libobjc.objc_msgSend(ns_win, sel("setLevel:"), level)

        # setHidesOnDeactivate
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        libobjc.objc_msgSend(ns_win, sel("setHidesOnDeactivate:"), not pinned)
        libobjc.objc_msgSend(ns_win, sel("setMovableByWindowBackground:"), True)

        # setCollectionBehavior: pinned=81(canJoinAllSpaces|stationary|ignoresCycle), unpinned=0(default)
        behavior = ctypes.c_ulong(81 if pinned else 0)
        libobjc.objc_msgSend.restype  = None
        libobjc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        libobjc.objc_msgSend(ns_win, sel("setCollectionBehavior:"), behavior)
    except Exception:
        pass


class DesktopWidget(QWidget):
    """
    半透明毛玻璃风格桌面浮动小窗口。
    - macOS NSVisualEffectView 模糊背景
    - 按钮切换"始终置顶"与"普通窗口"
    - 可拖动，位置跨会话记忆
    """

    closed = pyqtSignal()

    # 毛玻璃配色 — Qt 层半透明，NSVisualEffectView 模糊层透出
    _C_BG     = "rgba(10, 12, 22, 153)"    # 60% 深色膜
    _C_TITLE  = "rgba(255, 255, 255, 18)"  # 淡白标题栏
    _C_BORDER = "rgba(255, 255, 255, 30)"  # 微白边框
    _C_SEP    = "rgba(255, 255, 255, 20)"  # 分隔线
    _C_TEXT   = "#ebeff5"                     # 正文（实色，毛玻璃下 rgba 会不可见）
    _C_MUTED  = "#a0a8b9"                     # 次要文字
    _C_GREEN  = "#34d399"                  # A股绿跌
    _C_RED    = "#f87171"                  # A股红涨
    _C_AMBER  = "#fbbf24"
    _C_PIN_ON = "rgba(100, 160, 255, 220)" # 置顶激活蓝

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pinned = True  # 默认置顶
        self._drag_pos = None
        self._news_items: list[dict] = []
        self._quote_data: list[dict] = []
        self._vibrancy_applied = False

        self._t_signals: dict = {}   # {code: {"type": "buy"/"sell", "expires": float}}
        self._t_cleanup_timer = QTimer(self)
        self._t_cleanup_timer.timeout.connect(self._cleanup_t_signals)
        self._t_cleanup_timer.start(10_000)   # 每 10 秒清理一次过期 badge

        self._set_window_flags(pinned=True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(260)
        self.setMaximumWidth(320)

        self._build_ui()
        self._restore_position()

    # ── 窗口 flags ──

    def _set_window_flags(self, pinned: bool):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    # ── UI 构建 ──

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 整体容器（圆角 + 半透明背景，vibrancy 会叠加在下方）
        self._container = QWidget()
        self._container.setObjectName("wdg_container")
        self._container.setStyleSheet(f"""
            QWidget#wdg_container {{
                background-color: {self._C_BG};
                border: 1px solid {self._C_BORDER};
                border-radius: 14px;
            }}
        """)
        outer.addWidget(self._container)

        main = QVBoxLayout(self._container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ── 标题栏（_DragHandle 使标题区域整体可拖动）
        title_bar = _DragHandle()
        title_bar.setFixedHeight(36)
        title_bar.setObjectName("wdg_title")
        title_bar.setStyleSheet(f"""
            QWidget#wdg_title {{
                background-color: {self._C_TITLE};
                border-bottom: 1px solid {self._C_SEP};
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }}
        """)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(10, 0, 8, 0)
        tb.setSpacing(4)

        # Logo（QPixmap 贴图，规避 WA_TranslucentBackground 下 stylesheet 失效问题）
        logo_lbl = QLabel()
        logo_lbl.setPixmap(_make_radar_logo(20))
        logo_lbl.setFixedSize(20, 20)
        logo_lbl.setStyleSheet("background: transparent; border: none;")

        # 运行状态指示灯
        self._dot = QLabel("●")
        self._dot.setStyleSheet(
            f"color: {self._C_MUTED}; font-size: 8px; border: none;"
        )

        lbl_title = QLabel("财联社监控")
        lbl_title.setStyleSheet(
            f"color: {self._C_TEXT}; font-size: 12px; font-weight: 600; border: none;"
        )

        # 自绘置顶按钮
        self._btn_pin = _PinButton()
        self._btn_pin.clicked.connect(self._toggle_pin)

        # 自绘关闭按钮
        btn_close = _CloseButton()
        btn_close.clicked.connect(self._on_close)

        tb.addWidget(self._dot)
        tb.addSpacing(6)
        tb.addWidget(logo_lbl)
        tb.addSpacing(4)
        tb.addWidget(lbl_title)
        tb.addStretch()
        tb.addWidget(self._btn_pin)
        tb.addWidget(btn_close)
        main.addWidget(title_bar)

        # ── 内容区
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 10, 12, 12)
        cl.setSpacing(8)

        self._news_label = QLabel()
        self._news_label.setTextFormat(Qt.TextFormat.RichText)
        self._news_label.setWordWrap(True)
        self._news_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._news_label.setStyleSheet("background: transparent; border: none;")
        self._refresh_news_label()
        cl.addWidget(self._news_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {self._C_SEP}; border: none;")
        cl.addWidget(sep)

        self._quote_label = QLabel()
        self._quote_label.setTextFormat(Qt.TextFormat.RichText)
        self._quote_label.setWordWrap(True)
        self._quote_label.setStyleSheet("background: transparent; border: none;")
        self._refresh_quote_label()
        cl.addWidget(self._quote_label)

        main.addWidget(content)

    # ── 置顶切换 ──

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self._btn_pin.set_active(self._pinned)
        # macOS 上改 WindowFlags 必须先 hide()，否则新 flag 不生效
        self.hide()
        self._set_window_flags(self._pinned)
        self._vibrancy_applied = False
        self.show()

    # ── macOS 毛玻璃 ──

    def _try_vibrancy(self):
        if not self._vibrancy_applied:
            self._vibrancy_applied = _apply_macos_vibrancy(self)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._try_vibrancy)
        QTimer.singleShot(0, lambda: _fix_widget_float(self, self._pinned))

    # ── 数据更新 ──

    def update_news(self, rows: list[dict]):
        existing_ids = {r.get("ID") for r in self._news_items}
        for row in rows:
            if row.get("ID") not in existing_ids:
                self._news_items.insert(0, row)
                existing_ids.add(row.get("ID"))
        self._news_items = self._news_items[:8]
        self._refresh_news_label()

    def update_quotes(self, quotes: list[dict]):
        self._quote_data = quotes
        self._refresh_quote_label()

    def show_t_signal(self, code: str, sig_type: str):
        """显示做T信号 badge，60 秒后自动消失。"""
        import time
        self._t_signals[code] = {"type": sig_type, "expires": time.time() + 60}
        self._refresh_quote_label()

    def _cleanup_t_signals(self):
        import time
        now = time.time()
        expired = [c for c, v in self._t_signals.items() if v["expires"] <= now]
        if expired:
            for c in expired:
                del self._t_signals[c]
            self._refresh_quote_label()

    def set_running(self, running: bool):
        color = self._C_GREEN if running else self._C_MUTED
        self._dot.setStyleSheet(f"color: {color}; font-size: 8px; border: none;")

    # ── 渲染 ──

    def _refresh_news_label(self):
        if not self._news_items:
            self._news_label.setText(
                f"<span style='color:{self._C_MUTED};font-size:11px;'>等待新闻...</span>"
            )
            return
        lines = []
        for item in self._news_items[:6]:
            stocks = item.get("相关股票", "")
            if isinstance(stocks, float):
                stocks = ""
            _t = item.get("标题", "") or item.get("内容", "")
            if isinstance(_t, float):
                _t = ""
            title  = str(_t)[:30] if _t else ""
            raw_t  = item.get("发布时间", "")
            if isinstance(raw_t, float):
                raw_t = ""
            _rt = str(raw_t) if raw_t else ""
            _m  = re.search(r"(\d{2}:\d{2})", _rt)
            t_str = _m.group(1) if _m else ""

            if "↑" in stocks and "↓" not in stocks:
                c, icon = self._C_GREEN, "↑"
            elif "↓" in stocks and "↑" not in stocks:
                c, icon = self._C_RED, "↓"
            elif "↑" in stocks and "↓" in stocks:
                c, icon = self._C_AMBER, "⇅"
            else:
                c, icon = self._C_MUTED, "·"

            line = (
                f"<span style='color:{c};font-size:13px;font-weight:600;'>{icon}</span>"
                f"<span style='color:{self._C_TEXT};font-size:11px;'> {title}</span>"
            )
            if t_str:
                line += (
                    f"<span style='color:{self._C_MUTED};font-size:10px;'> {t_str}</span>"
                )
            lines.append(line)
        self._news_label.setText("<br>".join(lines))

    def _refresh_quote_label(self):
        if not self._quote_data:
            self._quote_label.setText(
                f"<span style='color:{self._C_MUTED};font-size:11px;'>暂无自选股报价</span>"
            )
            return
        lines = []
        for q in self._quote_data:
            name  = q.get("name", q.get("code", ""))
            price = q.get("price", "--")
            pct   = q.get("pct_change", "")
            try:
                pct_f = float(pct)
                sign  = "▲" if pct_f >= 0 else "▼"
                c     = self._C_RED if pct_f >= 0 else self._C_GREEN
                ps    = f"{sign}{abs(pct_f):.2f}%"
            except Exception:
                c, ps = self._C_MUTED, "--"
            sig = self._t_signals.get(q.get("code", ""))
            if sig:
                is_buy  = sig["type"] == "buy"
                bg_col  = self._C_RED if is_buy else self._C_GREEN
                badge   = "B" if is_buy else "S"
                badge_html = (
                    f"<span style='color:#fff;background-color:{bg_col};"
                    f"border-radius:2px;font-size:9px;font-weight:bold;"
                    f"padding:0 3px;'> {badge} </span>"
                )
            else:
                badge_html = ""
            line = (
                f"<span style='color:{self._C_MUTED};font-size:10px;'>{name}</span>"
                f"<span style='color:{self._C_TEXT};font-size:12px;font-weight:bold;'>"
                f" ¥{price}</span>"
                f"<span style='color:{c};font-size:11px;'> {ps}</span>"
                f"{badge_html}"
            )
            lines.append(line)
        self._quote_label.setText("<br>".join(lines))

    # ── 关闭 ──

    def _on_close(self):
        self._save_position()
        self.hide()
        self.closed.emit()

    # ── 拖动 ──

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._save_position()

    # ── 位置持久化 ──

    def _save_position(self):
        cfg = ConfigManager.load()
        cfg["widget_pos"] = [self.x(), self.y()]
        ConfigManager.save(cfg)

    def _restore_position(self):
        cfg = ConfigManager.load()
        pos = cfg.get("widget_pos")
        if pos and len(pos) == 2:
            self.move(pos[0], pos[1])
        else:
            from PyQt6.QtWidgets import QApplication as _App
            screen = _App.primaryScreen().geometry()
            self.move(screen.width() - 340, screen.height() - 420)


# ──────────────────────────────────────────
# 报价栏可拖拽 Chip
# ──────────────────────────────────────────

class _DraggableChip(QFrame):
    """
    报价栏股票 chip，支持横向拖拽排序。
    拖拽时鼠标变为抓手，释放后调用 save_order_cb() 保存新顺序。
    """

    def __init__(self, code: str, save_order_cb, parent=None):
        super().__init__(parent)
        self.code = code
        self._save_order = save_order_cb
        self._drag_start_x: float | None = None
        self._dragging = False
        self._ghost: QLabel | None = None
        self._ghost_offset = QPoint(0, 0)
        self.setObjectName("quote_chip")
        self.setStyleSheet(
            "QFrame#quote_chip { background-color: #F4F4F4;"
            "border: none; border-radius: 12px; }"
        )
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_x = event.position().x()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._drag_start_x is not None):
            dx = event.position().x() - self._drag_start_x
            if not self._dragging and abs(dx) > 6:
                self._dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self._start_ghost(event.globalPosition().toPoint())
            if self._dragging:
                self._move_ghost(event.globalPosition().toPoint())
                self._try_swap(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._end_ghost()
            self._save_order()
        self._drag_start_x = None
        self._dragging = False
        super().mouseReleaseEvent(event)

    def _start_ghost(self, global_pos: QPoint):
        """创建半透明截图跟随鼠标，原 chip 变虚。"""
        pixmap = self.grab()
        ghost = QLabel(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        ghost.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        ghost.setPixmap(pixmap)
        ghost.resize(pixmap.size())
        ghost.setWindowOpacity(0.75)
        ghost.show()
        self._ghost = ghost
        self._ghost_offset = QPoint(self.width() // 2, self.height() // 2)
        self._move_ghost(global_pos)
        # 原 chip 半透明
        fx = QGraphicsOpacityEffect()
        fx.setOpacity(0.35)
        self.setGraphicsEffect(fx)

    def _move_ghost(self, global_pos: QPoint):
        if self._ghost:
            self._ghost.move(global_pos - self._ghost_offset)

    def _end_ghost(self):
        if self._ghost:
            self._ghost.close()
            self._ghost.deleteLater()
            self._ghost = None
        self.setGraphicsEffect(None)

    def _try_swap(self, global_pos):
        container = self.parent()
        if container is None:
            return
        layout = container.layout()
        if layout is None:
            return
        local_x = container.mapFromGlobal(global_pos).x()
        n = layout.count() - 1  # 最后一项是 stretch，排除
        my_idx = -1
        for i in range(n):
            item = layout.itemAt(i)
            if item and item.widget() is self:
                my_idx = i
                break
        if my_idx == -1:
            return
        # 尝试与左侧 chip 交换
        if my_idx > 0:
            lw = layout.itemAt(my_idx - 1).widget()
            if lw and local_x < lw.x() + lw.width() * 0.5:
                layout.removeWidget(self)
                layout.insertWidget(my_idx - 1, self)
                return
        # 尝试与右侧 chip 交换
        if my_idx < n - 1:
            rw = layout.itemAt(my_idx + 1).widget()
            if rw and local_x > rw.x() + rw.width() * 0.5:
                layout.removeWidget(self)
                layout.insertWidget(my_idx + 1, self)


# ──────────────────────────────────────────
# MainWindow
# ──────────────────────────────────────────

class MainWindow(QMainWindow):
    TABLE_COLS = ["发布时间", "标题", "相关股票", "股票代码", "摘要", ""]
    TABLE_WIDTHS = [140, 260, 140, 120, 300, 44]

    def __init__(self):
        super().__init__()
        self.config = ConfigManager.load()
        self._thread: ScraperThread | None = None
        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._countdown_secs = 0
        self._is_running = False
        self._table_ids: set[str] = set()   # 已插入表格的 ID，防重复
        self._quote_thread: QuoteFetchThread | None = None
        self._quote_timer = QTimer(self)
        self._quote_timer.timeout.connect(self._refresh_quotes)
        self._quote_labels: dict[str, QLabel] = {}        # code → price label
        self._quote_badges: dict[str, QLabel] = {}        # code → T信号 badge
        self._quote_badge_timers: dict[str, QTimer] = {}  # code → 自动隐藏计时器
        self._stock_list: list[dict] = []                 # 全量股票列表（后台加载）
        self._stock_loader = StockListLoader()
        self._stock_loader.loaded.connect(self._on_stock_list_loaded)
        self._stock_loader.start()

        # 桌面浮动小组件
        self._desktop_widget = DesktopWidget()
        self._desktop_widget.closed.connect(self._on_widget_closed)

        self.setWindowTitle("财联社电报监控")
        self.setMinimumSize(1000, 640)
        self.resize(1200, 720)
        self.setStyleSheet(STYLESHEET)

        self._build_ui()
        self._load_config_to_ui()
        self._update_status(False)
        self._load_excel_to_table()

    def _load_excel_to_table(self):
        """启动时从 Excel 加载历史数据到表格"""
        excel_path = Path(self.config.get("excel_path", DEFAULTS["excel_path"]))
        if not excel_path.exists():
            return
        try:
            df = pd.read_excel(excel_path, dtype=str)
            if df.empty:
                return
            if "发布时间" in df.columns:
                df.sort_values("发布时间", ascending=False, inplace=True, ignore_index=True)
            rows = df.to_dict(orient="records")
            self._on_new_data(rows)
        except Exception:
            import traceback; traceback.print_exc()

    # ── UI 构建 ────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部栏
        root.addWidget(self._build_topbar())

        # 报价栏
        root.addWidget(self._build_quotebar())

        # 主体分割
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(
            "QSplitter::handle { background: transparent; border: none; }"
        )
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_content())
        splitter.setSizes([240, 960])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            "background-color: #FFFFFF;"
            "border-bottom: 1px solid #F0F0F0;"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 16, 0)
        layout.setSpacing(8)

        logo = QLabel()
        logo.setPixmap(_make_radar_logo(18))
        logo.setFixedSize(18, 18)

        title = QLabel("财联社监控")
        title.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {COLOR_TEXT};"
        )
        layout.addWidget(logo)
        layout.addSpacing(6)
        layout.addWidget(title)
        layout.addStretch()

        self.lbl_status_dot = QLabel("●")
        self.lbl_status_dot.setStyleSheet(f"font-size: 10px; color: {COLOR_MUTED};")
        self.lbl_status_text = QLabel("已停止")
        self.lbl_status_text.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 13px;")
        self.lbl_countdown = QLabel("")
        self.lbl_countdown.setStyleSheet(
            f"color: {COLOR_MUTED}; font-family: 'SF Mono', monospace;"
            f"font-size: 12px; min-width: 72px;"
        )

        layout.addWidget(self.lbl_status_dot)
        layout.addSpacing(4)
        layout.addWidget(self.lbl_status_text)
        layout.addSpacing(12)
        layout.addWidget(self.lbl_countdown)

        # 桌面小组件开关按钮
        self.btn_widget = QPushButton("小组件")
        self.btn_widget.setCheckable(True)
        self.btn_widget.setFixedHeight(28)
        self.btn_widget.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: none;
                border-radius: 4px;
                color: {COLOR_MUTED};
                padding: 0 12px;
                font-size: 12px;
                font-weight: 400;
            }}
            QPushButton:hover {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_TEXT};
            }}
            QPushButton:checked {{
                background-color: {COLOR_SEL};
                color: {COLOR_ACCENT};
            }}
        """)
        self.btn_widget.toggled.connect(self._toggle_desktop_widget)
        layout.addWidget(self.btn_widget)

        # AI 设置按钮
        btn_ai_settings = QPushButton("⚙ 设置")
        btn_ai_settings.setFixedHeight(28)
        btn_ai_settings.setToolTip("配置 AI API 提供商")
        btn_ai_settings.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: none;
                border-radius: 4px;
                color: {COLOR_MUTED};
                padding: 0 12px;
                font-size: 12px;
                font-weight: 400;
            }}
            QPushButton:hover {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_TEXT};
            }}
        """)
        btn_ai_settings.clicked.connect(self._open_ai_settings)
        layout.addWidget(btn_ai_settings)

        return bar

    def _build_quotebar(self) -> QWidget:
        from PyQt6.QtWidgets import QScrollArea
        bar = QFrame()
        bar.setFixedHeight(46)
        bar.setStyleSheet("background-color: #FFFFFF;")
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(20, 0, 12, 0)
        outer.setSpacing(8)

        # 下拉建议列表（先创建，供 SearchLineEdit 引用）
        self._suggest_list = QListWidget(self)
        self._suggest_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._suggest_list.setFixedWidth(200)
        self._suggest_list.hide()
        self._suggest_list.itemClicked.connect(self._on_suggestion_clicked)
        self._suggest_list.itemActivated.connect(self._on_suggestion_clicked)

        # 输入框（传入 suggest_list 引用）
        self.quote_input = SearchLineEdit(self._suggest_list)
        self.quote_input.setPlaceholderText("代码 / 名称 / 拼音…")
        self.quote_input.setFixedWidth(200)
        self.quote_input.setFixedHeight(30)
        self.quote_input.returnPressed.connect(self._add_watch_code)
        self.quote_input.textChanged.connect(self._on_quote_input_changed)
        self.quote_input.editingFinished.connect(
            lambda: QTimer.singleShot(150, self._suggest_list.hide)
        )

        outer.addWidget(self.quote_input)
        outer.addSpacing(12)

        # 可横向滚动的报价区域
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(46)
        scroll.setStyleSheet("background: transparent;")

        self._quote_container = QWidget()
        self._quote_container.setStyleSheet("background: transparent;")
        self._quote_row = QHBoxLayout(self._quote_container)
        self._quote_row.setContentsMargins(0, 0, 0, 0)
        self._quote_row.setSpacing(10)
        self._quote_row.addStretch()
        scroll.setWidget(self._quote_container)
        outer.addWidget(scroll, 1)

        # 刷新按钮
        btn_refresh = QPushButton("刷新")
        btn_refresh.setFixedHeight(28)
        btn_refresh.setToolTip("立即刷新报价")
        btn_refresh.setStyleSheet(
            f"background-color: transparent; color: {COLOR_TEXT};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 4px; font-size: 12px; padding: 0 8px;"
        )
        btn_refresh.clicked.connect(self._refresh_quotes)
        outer.addWidget(btn_refresh)

        # 启动定时刷新，并加载已保存的自选股
        for code in self.config.get("watch_codes", []):
            self._add_quote_chip(code)
        if self.config.get("watch_codes"):
            self._refresh_quotes()
        refresh = self.config.get("quote_refresh_secs", 30)
        self._quote_timer.start(refresh * 1000)

        return bar

    def _on_stock_list_loaded(self, stock_list: list):
        self._stock_list = stock_list
        self.quote_input.setPlaceholderText("代码 / 名称 / 拼音首字母…")
        if hasattr(self, "intraday_tab"):
            self.intraday_tab.set_stocks(self.config.get("watch_codes", []), stock_list)

    def _on_quote_input_changed(self, text: str):
        text = text.strip()
        if not text or not self._stock_list:
            self._suggest_list.hide()
            return
        matches = self._search_stocks(text)
        if not matches:
            self._suggest_list.hide()
            return
        self._suggest_list.clear()
        for s in matches[:10]:
            self._suggest_list.addItem(f"{s['code']}  {s['name']}")
        # 定位到输入框正下方（坐标相对于 MainWindow）
        pos = self.quote_input.mapTo(self, self.quote_input.rect().bottomLeft())
        row_h = self._suggest_list.sizeHintForRow(0) + 2
        self._suggest_list.setFixedHeight(min(len(matches), 10) * row_h + 4)
        self._suggest_list.setGeometry(pos.x(), pos.y(), 200,
                                       min(len(matches), 10) * row_h + 4)
        self._suggest_list.raise_()
        self._suggest_list.show()

    def _search_stocks(self, query: str) -> list[dict]:
        q = query.lower().strip()
        results = []
        for s in self._stock_list:
            if (s["code"].startswith(q)
                    or q in s["name"]
                    or s["pinyin"].startswith(q)):
                results.append(s)
                if len(results) >= 10:
                    break
        return results

    def _on_suggestion_clicked(self, item):
        code = item.text().split()[0]
        self._suggest_list.hide()
        self.quote_input.clear()
        self._add_watch_code_silent(code)
        self._refresh_quotes()

    def _add_watch_code(self):
        text = self.quote_input.text().strip()
        if not text:
            return
        # 如果是6位数字，直接添加
        if re.match(r"^\d{6}$", text):
            self._add_watch_code_silent(text)
            self.quote_input.clear()
            self._suggest_list.hide()
            self._refresh_quotes()
            return
        # 否则从列表里精确匹配名称或拼音，取第一个结果
        matches = self._search_stocks(text)
        if matches:
            self._add_watch_code_silent(matches[0]["code"])
            self.quote_input.clear()
            self._suggest_list.hide()
            self._refresh_quotes()
        else:
            self.status_bar.showMessage("未找到匹配股票，请输入6位代码", 2000)

    def _save_watch_order(self):
        """从 layout 当前顺序重建 watch_codes 并保存。"""
        n = self._quote_row.count() - 1  # 最后是 stretch
        codes = []
        for i in range(n):
            item = self._quote_row.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, _DraggableChip):
                codes.append(w.code)
        self.config["watch_codes"] = codes
        ConfigManager.save(self.config)

    def _add_quote_chip(self, code: str):
        """在报价栏添加一个股票 chip（先占位，等数据回来再更新）"""
        chip = _DraggableChip(code, self._save_watch_order)
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(10, 0, 6, 0)
        chip_layout.setSpacing(4)

        lbl = QLabel(f"{code}  --")
        lbl.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 12px; border: none;")
        chip_layout.addWidget(lbl)

        # 做T信号 badge（默认隐藏，信号触发时显示 B/S）
        badge = QLabel("")
        badge.setFixedHeight(16)
        badge.setStyleSheet(
            "font-size: 10px; font-weight: bold; padding: 0 4px;"
            "border-radius: 3px; border: none;"
        )
        badge.setVisible(False)
        chip_layout.addWidget(badge)
        self._quote_badges[code] = badge

        btn_close = QPushButton("×")
        btn_close.setFixedSize(16, 16)
        btn_close.setStyleSheet(
            f"background: transparent; color: {COLOR_MUTED}; border: none;"
            f"font-size: 13px; padding: 0;"
        )
        btn_close.setCursor(Qt.CursorShape.ArrowCursor)
        def make_remover(c):
            def remove():
                self._remove_watch_code(c)
            return remove
        btn_close.clicked.connect(make_remover(code))
        chip_layout.addWidget(btn_close)

        # 插在 stretch 之前
        self._quote_row.insertWidget(self._quote_row.count() - 1, chip)
        self._quote_labels[code] = lbl

    def _remove_watch_code(self, code: str):
        codes = self.config.get("watch_codes", [])
        if code in codes:
            codes.remove(code)
        self.config["watch_codes"] = codes
        ConfigManager.save(self.config)
        self._quote_badges.pop(code, None)
        t = self._quote_badge_timers.pop(code, None)
        if t:
            t.stop()
        lbl = self._quote_labels.pop(code, None)
        if lbl:
            try:
                chip = lbl.parent()
                if chip is not None:
                    self._quote_row.removeWidget(chip)
                    chip.deleteLater()
            except RuntimeError:
                pass  # C++ 对象已被删除，忽略
        if hasattr(self, "intraday_tab"):
            self.intraday_tab.set_stocks(codes, self._stock_list)

    def _add_codes_to_watchbar(self, codes: list[str]):
        added = []
        for code in codes:
            if code not in self.config.get("watch_codes", []):
                self._add_watch_code_silent(code)
                added.append(code)
        if added:
            self._refresh_quotes()
            self.status_bar.showMessage(f"已添加到报价栏: {' '.join(added)}", 3000)
        else:
            self.status_bar.showMessage("股票已在报价栏中", 2000)

    def _add_watch_code_silent(self, code: str):
        """不弹提示，直接添加代码到报价栏"""
        codes = self.config.get("watch_codes", [])
        if code in codes:
            return
        codes.append(code)
        self.config["watch_codes"] = codes
        ConfigManager.save(self.config)
        self._add_quote_chip(code)
        if hasattr(self, "intraday_tab"):
            self.intraday_tab.set_stocks(codes, self._stock_list)

    def _refresh_quotes(self):
        codes = self.config.get("watch_codes", [])
        if not codes:
            return
        if self._quote_thread and self._quote_thread.isRunning():
            return
        self._quote_thread = QuoteFetchThread(codes)
        self._quote_thread.quotes_ready.connect(self._on_quotes_ready)
        self._quote_thread.start()

    def _on_quotes_ready(self, results: list):
        for q in results:
            code = q["code"]
            lbl = self._quote_labels.get(code)
            if not lbl:
                continue
            price = q["price"]
            pct   = q["pct_change"]
            try:
                pct_f = float(pct)
                sign  = "▲" if pct_f >= 0 else "▼"
                text_color = COLOR_RED if pct_f >= 0 else COLOR_GREEN  # A股红涨绿跌
                pct_str = f"{sign}{abs(pct_f):.2f}%"
            except Exception:
                text_color = COLOR_MUTED
                pct_str = "--"
            name = q.get("name", code)
            lbl.setText(f"{name}  ¥{price}  {pct_str}")
            lbl.setStyleSheet(
                f"color: {text_color}; font-size: 12px; border: none; font-weight: bold;"
            )
        # 同步到桌面小组件
        self._desktop_widget.update_quotes(results)

    # ── 报价胶囊做T信号 badge ─────────────────

    def _show_t_signal_badge(self, code: str, sig_type: str):
        """在对应胶囊上显示 B/S badge，60 秒后自动隐藏。"""
        badge = self._quote_badges.get(code)
        if not badge:
            return
        is_buy = (sig_type == "buy")
        text  = "B" if is_buy else "S"
        bg    = COLOR_RED if is_buy else COLOR_GREEN
        badge.setText(text)
        badge.setStyleSheet(
            f"color: white; background-color: {bg}; font-size: 10px;"
            f"font-weight: bold; padding: 0 4px; border-radius: 3px; border: none;"
        )
        badge.setVisible(True)
        # 重置/启动 60 秒自动隐藏计时器
        old = self._quote_badge_timers.pop(code, None)
        if old:
            old.stop()
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(lambda: self._hide_t_signal_badge(code))
        t.start(60_000)
        self._quote_badge_timers[code] = t

    def _hide_t_signal_badge(self, code: str):
        badge = self._quote_badges.get(code)
        if badge:
            badge.setVisible(False)
        self._quote_badge_timers.pop(code, None)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(240)
        sidebar.setStyleSheet("background-color: #F7F7F7;")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(4)

        # 基本设置
        grp_basic = QGroupBox("基本设置")
        grp_layout = QVBoxLayout(grp_basic)
        grp_layout.setSpacing(6)
        grp_layout.setContentsMargins(0, 4, 0, 4)

        self.spin_interval = self._make_spinbox(1, 60,  self.config["interval_min"],        "分钟")
        self.spin_scroll   = self._make_spinbox(1, 20,  self.config["scroll_times"],          "次")
        self.spin_timeout  = self._make_spinbox(5, 120, self.config["wait_timeout"],           "秒")
        self.spin_quote    = self._make_spinbox(3, 300, self.config.get("quote_refresh_secs", 30), "秒")

        grp_layout.addLayout(self._labeled_row("监控频率", self.spin_interval))
        grp_layout.addLayout(self._labeled_row("加载次数", self.spin_scroll))
        grp_layout.addLayout(self._labeled_row("等待超时", self.spin_timeout))
        grp_layout.addLayout(self._labeled_row("报价刷新", self.spin_quote))
        layout.addWidget(grp_basic)

        # 存储设置
        grp_store = QGroupBox("存储设置")
        grp_store_layout = QVBoxLayout(grp_store)
        grp_store_layout.setContentsMargins(0, 4, 0, 4)
        self.edit_excel = QLineEdit(self.config["excel_path"])
        self.edit_excel.setPlaceholderText("Excel 保存路径")
        btn_browse = QPushButton("浏览")
        btn_browse.setObjectName("btn_browse")
        btn_browse.setFixedWidth(48)
        btn_browse.clicked.connect(self._browse_excel)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.edit_excel)
        row.addWidget(btn_browse)
        grp_store_layout.addLayout(row)
        layout.addWidget(grp_store)

        # AI 设置
        grp_ai = QGroupBox("AI 设置")
        grp_ai_layout = QVBoxLayout(grp_ai)
        grp_ai_layout.setSpacing(6)
        grp_ai_layout.setContentsMargins(0, 4, 0, 4)

        self.chk_ai     = _ToggleSwitch("启用 AI 分析")
        self.chk_all    = _ToggleSwitch("记录无利好条目")
        self.chk_ai.setChecked(True)
        self.chk_all.setChecked(self.config.get("analyze_all", True))

        self.lbl_ai_provider = QLabel(self._get_provider_display())
        self.lbl_ai_provider.setStyleSheet(
            f"color: {COLOR_MUTED}; font-size: 11px; margin-top: 4px;"
        )

        btn_ai_cfg = QPushButton("配置 AI API")
        btn_ai_cfg.setFixedHeight(28)
        btn_ai_cfg.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                color: {COLOR_TEXT};
                font-size: 12px;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                background-color: {COLOR_SURFACE};
            }}
        """)
        btn_ai_cfg.clicked.connect(self._open_ai_settings)

        grp_ai_layout.addSpacing(4)
        grp_ai_layout.addWidget(self.chk_ai)
        grp_ai_layout.addSpacing(8)
        grp_ai_layout.addWidget(self.chk_all)
        grp_ai_layout.addSpacing(4)
        grp_ai_layout.addWidget(self.lbl_ai_provider)
        grp_ai_layout.addWidget(btn_ai_cfg)
        layout.addWidget(grp_ai)

        layout.addSpacing(16)

        # 按钮
        self.btn_start = QPushButton("开始监控")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self._start_loop)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)

        self.btn_once = QPushButton("立即执行")
        self.btn_once.setObjectName("btn_once")
        self.btn_once.clicked.connect(self._run_once)

        self.btn_excel = QPushButton("打开 Excel")
        self.btn_excel.setObjectName("btn_excel")
        self.btn_excel.clicked.connect(self._open_excel)

        for btn in [self.btn_start, self.btn_stop, self.btn_once, self.btn_excel]:
            btn.setFixedHeight(34)
            layout.addWidget(btn)
            layout.addSpacing(2)

        layout.addStretch()
        return sidebar

    def _build_content(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 12, 16, 12)

        self.tabs = QTabWidget()

        # 运行日志 Tab
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("运行日志将显示在此处...")
        self.tabs.addTab(self.log_edit, "运行日志")

        # 最新数据 Tab
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.TABLE_COLS))
        self.table.setHorizontalHeaderLabels(self.TABLE_COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.table.cellDoubleClicked.connect(self._copy_cell)
        for i, w in enumerate(self.TABLE_WIDTHS):
            self.table.setColumnWidth(i, w)
        self.tabs.addTab(self.table, "最新数据")

        # 分时 T 做 Tab
        self.intraday_tab = IntradayMonitorTab()
        self.intraday_tab.signal_triggered.connect(self._show_t_signal_badge)
        self.intraday_tab.signal_triggered.connect(self._desktop_widget.show_t_signal)
        self.tabs.addTab(self.intraday_tab, "分时T做")
        self.intraday_tab.set_stocks(self.config.get("watch_codes", []), self._stock_list)

        layout.addWidget(self.tabs)
        return widget

    # ── 辅助 UI 构建 ──────────────────────

    def _make_spinbox(self, min_val, max_val, value, suffix="") -> QSpinBox:
        sb = QSpinBox()
        sb.setRange(min_val, max_val)
        sb.setValue(value)
        if suffix:
            sb.setSuffix(f" {suffix}")
        sb.setFixedHeight(28)
        sb.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        return sb

    def _labeled_row(self, label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 12px;")
        lbl.setFixedWidth(54)
        row.addWidget(lbl)
        row.addWidget(widget)
        return row

    # ── 配置读写 ──────────────────────────

    def _load_config_to_ui(self):
        self.spin_interval.setValue(self.config.get("interval_min", 5))
        self.spin_scroll.setValue(self.config.get("scroll_times", 3))
        self.spin_timeout.setValue(self.config.get("wait_timeout", 20))
        self.spin_quote.setValue(self.config.get("quote_refresh_secs", 30))
        self.edit_excel.setText(self.config.get("excel_path", DEFAULTS["excel_path"]))
        self.chk_all.setChecked(self.config.get("analyze_all", True))
        self.lbl_ai_provider.setText(self._get_provider_display())

    def _collect_config(self) -> dict:
        cfg = dict(self.config)
        cfg["interval_min"]       = self.spin_interval.value()
        cfg["scroll_times"]       = self.spin_scroll.value()
        cfg["wait_timeout"]       = self.spin_timeout.value()
        cfg["quote_refresh_secs"] = self.spin_quote.value()
        cfg["excel_path"]         = self.edit_excel.text().strip()
        cfg["analyze_all"]        = self.chk_all.isChecked()
        cfg["chrome_bin"]         = DEFAULTS["chrome_bin"]
        # AI 配置字段由 AISettingsDialog 管理，这里直接透传 self.config 中的值
        for key in ("ai_provider", "ai_api_key", "ai_model", "ai_base_url", "claude_bin"):
            cfg.setdefault(key, self.config.get(key, DEFAULTS.get(key, "")))
        return cfg

    # ── 状态更新 ──────────────────────────

    def _update_status(self, running: bool):
        self._is_running = running
        if running:
            self.lbl_status_dot.setStyleSheet(f"font-size: 10px; color: {COLOR_GREEN};")
            self.lbl_status_dot.setText("●")
            self.lbl_status_text.setStyleSheet(f"color: {COLOR_GREEN}; font-size: 13px;")
            self.lbl_status_text.setText("运行中")
        else:
            self.lbl_status_dot.setStyleSheet(f"font-size: 10px; color: {COLOR_MUTED};")
            self.lbl_status_dot.setText("●")
            self.lbl_status_text.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 13px;")
            self.lbl_status_text.setText("已停止")
            self.lbl_countdown.setText("")
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_once.setEnabled(not running)
        self._desktop_widget.set_running(running)

    # ── 倒计时 ────────────────────────────

    def _start_countdown(self, seconds: int):
        self._countdown_secs = seconds
        self._countdown_timer.start(1000)
        self._tick_countdown()

    def _tick_countdown(self):
        if self._countdown_secs <= 0:
            self._countdown_timer.stop()
            self.lbl_countdown.setText("")
            return
        m, s = divmod(self._countdown_secs, 60)
        self.lbl_countdown.setText(f"下次: {m:02d}:{s:02d}")
        self._countdown_secs -= 1

    # ── 信号处理 ──────────────────────────

    def _on_log_message(self, text: str, level: str):
        try:
            cursor = self.log_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)

            fmt = QTextCharFormat()
            if level == "good":
                fmt.setForeground(QColor(COLOR_GREEN))
            elif level == "error":
                fmt.setForeground(QColor(COLOR_RED))
            else:
                fmt.setForeground(QColor(COLOR_TEXT))

            cursor.setCharFormat(fmt)
            cursor.insertText(text + "\n")
            self.log_edit.setTextCursor(cursor)
            self.log_edit.ensureCursorVisible()
        except Exception:
            import traceback; traceback.print_exc()

    def _on_new_data(self, rows: list):
        inserted = []
        try:
            self.table.setSortingEnabled(False)
            for row_dict in reversed(rows):
                rid = str(row_dict.get("ID", ""))
                if rid and rid in self._table_ids:
                    continue
                self._insert_table_row(row_dict)
                if rid:
                    self._table_ids.add(rid)
                inserted.append(row_dict)
            self.table.setSortingEnabled(True)
            self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        except Exception:
            import traceback; traceback.print_exc()
        # 同步到桌面小组件（仅真正新插入的）
        try:
            if inserted:
                self._desktop_widget.update_news(inserted)
        except Exception:
            import traceback; traceback.print_exc()

    @staticmethod
    def _sv(v) -> str:
        """安全转字符串，处理 pandas NaN（float）"""
        if v is None:
            return ""
        if isinstance(v, float) and v != v:   # NaN != NaN
            return ""
        return str(v)

    def _insert_table_row(self, row_dict: dict):
        sv = self._sv
        self.table.insertRow(0)
        stocks_text = sv(row_dict.get("相关股票", ""))
        data = [
            sv(row_dict.get("发布时间", "")),
            (sv(row_dict.get("标题", "")) + sv(row_dict.get("内容", "")))[:80],
            stocks_text,
            sv(row_dict.get("股票代码", "")),
            sv(row_dict.get("AI分析", "")),
        ]
        has_bullish = "↑" in stocks_text
        has_bearish = "↓" in stocks_text
        no_relevant = stocks_text == "无相关股票" or not stocks_text

        for col, val in enumerate(data):
            item = QTableWidgetItem(sv(val))
            if col == 2:
                if no_relevant:
                    item.setForeground(QColor(COLOR_MUTED))
                elif has_bullish and has_bearish:
                    item.setForeground(QColor(COLOR_ORANGE))
                elif has_bullish:
                    item.setForeground(QColor(COLOR_RED))   # 利好 → A股红
                elif has_bearish:
                    item.setForeground(QColor(COLOR_GREEN)) # 利空 → A股绿
            self.table.setItem(0, col, item)

        # 最后一列：添加到报价栏按钮（有股票代码时才显示）
        codes_raw = row_dict.get("股票代码", "")
        if isinstance(codes_raw, float):
            codes_raw = ""
        codes = [c.strip() for c in codes_raw.split("\n") if c.strip()] if codes_raw else []
        if codes:
            btn = _AddButton()
            btn.setToolTip("添加到报价栏")
            def make_adder(c_list):
                def add():
                    self._add_codes_to_watchbar(c_list)
                return add
            btn.clicked.connect(make_adder(codes))
            # 居中放入 cell
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_layout.addWidget(btn)
            self.table.setCellWidget(0, 5, cell)

        # 自动调整行高以显示多行代码
        self.table.resizeRowToContents(0)

    def _on_job_finished(self, added: int, total: int):
        self.status_bar.showMessage(
            f"本次新增 {added} 条 | 累计 {total} 条 | {now()}"
        )
        if self._is_running:
            interval_sec = self.spin_interval.value() * 60
            self._start_countdown(interval_sec)

    def _on_thread_done(self):
        self._update_status(False)
        self._countdown_timer.stop()
        self.lbl_countdown.setText("")

    # ── 按钮动作 ──────────────────────────

    def _start_loop(self):
        cfg = self._collect_config()
        ConfigManager.save(cfg)
        self.config = cfg
        # 报价刷新间隔可能已改变，重启 timer
        self._quote_timer.start(cfg["quote_refresh_secs"] * 1000)
        self._launch_thread("loop")
        self._update_status(True)
        self._on_log_message(f"[{now()}] 监控已启动（每 {cfg['interval_min']} 分钟）", "normal")

    def _run_once(self):
        cfg = self._collect_config()
        ConfigManager.save(cfg)
        self.config = cfg
        self._launch_thread("once")
        self._update_status(True)
        self._on_log_message(f"[{now()}] 开始单次执行...", "normal")

    def _stop(self):
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self.status_bar.showMessage("正在停止...")
        self._countdown_timer.stop()
        self.lbl_countdown.setText("")
        self._update_status(False)

    def _launch_thread(self, mode: str):
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)

        self._thread = ScraperThread(dict(self.config), mode)
        self._thread.log_message.connect(self._on_log_message)
        self._thread.new_data.connect(self._on_new_data)
        self._thread.job_finished.connect(self._on_job_finished)
        self._thread.finished.connect(self._on_thread_done)
        self._thread.start()

    def _browse_excel(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "选择 Excel 保存路径",
            self.edit_excel.text(),
            "Excel 文件 (*.xlsx)",
        )
        if path:
            self.edit_excel.setText(path)

    def _open_excel(self):
        path = self.edit_excel.text().strip()
        if path and Path(path).exists():
            subprocess.Popen(["open", path])
        else:
            self.status_bar.showMessage(f"文件不存在: {path}")

    def _copy_cell(self, row: int, col: int):
        item = self.table.item(row, col)
        if not item or not item.text():
            return

        # 股票代码列（col 3）且有多个代码时，弹出选择框
        if col == 3:
            codes = [c.strip() for c in item.text().split("\n") if c.strip()]
            if len(codes) > 1:
                self._show_code_picker(codes)
                return

        QApplication.clipboard().setText(item.text())
        self.status_bar.showMessage(f"已复制: {item.text()[:60]}", 2000)

    def _show_code_picker(self, codes: list[str]):
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel
        dlg = QDialog(self)
        dlg.setWindowTitle("选择要复制的代码")
        dlg.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: {COLOR_PANEL};
                border: 1px solid {COLOR_BORDER};
                border-radius: 6px;
            }}
            QPushButton {{
                background-color: {COLOR_INPUT_BG};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                padding: 6px 20px;
                font-family: "Menlo", "Monaco", monospace;
                font-size: 14px;
                font-weight: bold;
                text-align: center;
            }}
            QPushButton:hover {{
                background-color: {COLOR_BLUE_DIM};
                border-color: {COLOR_BLUE};
            }}
        """)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        lbl = QLabel("点击复制单个代码")
        lbl.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
        for code in codes:
            btn = QPushButton(code)
            btn.setFixedHeight(34)
            def make_handler(c):
                def handler():
                    QApplication.clipboard().setText(c)
                    self.status_bar.showMessage(f"已复制: {c}", 2000)
                    dlg.close()
                return handler
            btn.clicked.connect(make_handler(code))
            layout.addWidget(btn)

        # 在鼠标位置附近弹出
        from PyQt6.QtGui import QCursor
        dlg.adjustSize()
        pos = QCursor.pos()
        dlg.move(pos.x() - dlg.width() // 2, pos.y() - 10)
        dlg.exec()

    # ── 桌面小组件控制 ──────────────────

    def _toggle_desktop_widget(self, checked: bool):
        if checked:
            self._desktop_widget.show()
            self._desktop_widget.raise_()
        else:
            self._desktop_widget.hide()

    def _on_widget_closed(self):
        """小组件被用户关闭时，同步按钮状态"""
        self.btn_widget.setChecked(False)

    # ── AI 设置 ────────────────────────────

    def _get_provider_display(self) -> str:
        provider = self.config.get("ai_provider", "claude_cli")
        labels = dict(AISettingsDialog.PROVIDERS)
        name = labels.get(provider, provider)
        return f"当前: {name}"

    def _open_ai_settings(self):
        dlg = AISettingsDialog(self.config, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cfg = dlg.get_config()
            self.config.update(new_cfg)
            ConfigManager.save(self.config)
            self.lbl_ai_provider.setText(self._get_provider_display())
            self.status_bar.showMessage("AI 设置已保存", 2000)

    # ── 关闭事件 ──────────────────────────

    def closeEvent(self, event):
        cfg = self._collect_config()
        ConfigManager.save(cfg)
        self._quote_timer.stop()
        if self._quote_thread and self._quote_thread.isRunning():
            self._quote_thread.wait(2000)
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        self._desktop_widget.close()
        event.accept()


# ──────────────────────────────────────────
# 入口
# ──────────────────────────────────────────

def main():
    import sys
    import traceback as _tb

    # PyQt6 默认在 slot 中抛出未捕获异常时调用 abort()。
    # 设置 sys.excepthook 后 PyQt6 6.x 会改为记录异常并继续运行，防止闪退。
    def _safe_excepthook(exc_type, exc_val, exc_tb):
        if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            sys.__excepthook__(exc_type, exc_val, exc_tb)
            return
        _tb.print_exception(exc_type, exc_val, exc_tb)

    sys.excepthook = _safe_excepthook

    app = QApplication(sys.argv)
    app.setApplicationName("财联社监控")
    from PyQt6.QtGui import QIcon
    app.setWindowIcon(QIcon(_make_radar_logo(256, dock=True)))

    # 高分辨率支持
    try:
        from PyQt6.QtCore import Qt as _Qt
        app.setAttribute(_Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    except Exception:
        pass

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
