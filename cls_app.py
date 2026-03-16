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
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QSpinBox, QLineEdit, QPushButton, QTextEdit,
    QTableWidget, QTableWidgetItem, QTabWidget, QFileDialog,
    QHeaderView, QSplitter, QStatusBar, QFrame,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize,
)
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QClipboard


# ──────────────────────────────────────────
# 常量
# ──────────────────────────────────────────

URL = "https://www.cls.cn/telegraph"

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
}

QUOTE_REFRESH_SECS = 30  # 报价刷新间隔（秒）

# 暗色主题颜色
COLOR_BG        = "#0f172a"
COLOR_PANEL     = "#1e293b"
COLOR_BORDER    = "#334155"
COLOR_TEXT      = "#e2e8f0"
COLOR_MUTED     = "#94a3b8"
COLOR_GREEN     = "#22c55e"
COLOR_GREEN_DIM = "#166534"
COLOR_RED       = "#ef4444"
COLOR_RED_DIM   = "#7f1d1d"
COLOR_BLUE      = "#3b82f6"
COLOR_BLUE_DIM  = "#1e40af"
COLOR_EXEC_DIM  = "#065f46"
COLOR_INPUT_BG  = "#0f172a"


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
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )


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


def fetch_items(driver: webdriver.Chrome, config: dict, log_fn=None) -> list[dict]:
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


def analyze_news(title: str, body: str, config: dict, log_fn=None) -> dict | None:
    news_text = f"{title}{body}".strip()
    if not news_text:
        return None

    claude_bin = config.get("claude_bin", "") or "claude"
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # 确保 claude_bin 所在目录在 PATH 中
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


def enrich_with_ai(df: pd.DataFrame, config: dict, log_fn=None, row_fn=None) -> pd.DataFrame:
    """AI 批量分析；row_fn(row_dict) 每条分析完后回调（用于实时 emit）"""
    mask = df["AI分析时间"].isna() | (df["AI分析时间"] == "")
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
        analysis = analyze_news(title, body, config, log_fn)
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

        # 实时回调
        if row_fn:
            updated = df.loc[idx].to_dict()
            row_fn([updated])

        time.sleep(0.3)

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
        driver = build_driver(config)
        new_items = fetch_items(driver, config, log_fn)
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

        if old_df.empty:
            combined = new_df.copy()
        else:
            for col in new_df.columns:
                if col not in old_df.columns:
                    old_df[col] = ""
            combined = pd.concat([old_df, new_df], ignore_index=True)
            combined.drop_duplicates(subset=["ID"], keep="first", inplace=True)

        added = len(new_df)
        combined = enrich_with_ai(combined, config, log_fn, row_fn)

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
    return "sh" if code.startswith("6") else "sz"


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
            pass


# ──────────────────────────────────────────
# ScraperThread
# ──────────────────────────────────────────

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
# 样式表
# ──────────────────────────────────────────

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: "PingFang SC", "Helvetica Neue", Arial;
    font-size: 13px;
}}
QGroupBox {{
    background-color: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    margin-top: 8px;
    padding: 8px 6px 6px 6px;
    font-weight: bold;
    color: {COLOR_MUTED};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {COLOR_MUTED};
    font-size: 12px;
}}
QLabel {{
    color: {COLOR_TEXT};
}}
QSpinBox, QLineEdit {{
    background-color: {COLOR_INPUT_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 3px 6px;
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_BLUE_DIM};
}}
QSpinBox:focus, QLineEdit:focus {{
    border: 1px solid {COLOR_BLUE};
}}
QPushButton {{
    border: none;
    border-radius: 4px;
    padding: 6px 10px;
    font-weight: bold;
    color: white;
}}
QPushButton#btn_start {{
    background-color: {COLOR_BLUE_DIM};
}}
QPushButton#btn_start:hover {{
    background-color: {COLOR_BLUE};
}}
QPushButton#btn_stop {{
    background-color: {COLOR_RED_DIM};
}}
QPushButton#btn_stop:hover {{
    background-color: {COLOR_RED};
}}
QPushButton#btn_once {{
    background-color: {COLOR_EXEC_DIM};
}}
QPushButton#btn_once:hover {{
    background-color: #059669;
}}
QPushButton#btn_excel {{
    background-color: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    color: {COLOR_TEXT};
}}
QPushButton#btn_excel:hover {{
    background-color: {COLOR_BORDER};
}}
QPushButton#btn_browse {{
    background-color: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    color: {COLOR_MUTED};
    padding: 3px 8px;
    font-size: 11px;
    font-weight: normal;
}}
QPushButton#btn_browse:hover {{
    background-color: {COLOR_BORDER};
}}
QTextEdit {{
    background-color: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    color: {COLOR_TEXT};
    font-family: "Menlo", "Monaco", "Courier New";
    font-size: 12px;
}}
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    background-color: {COLOR_PANEL};
    border-radius: 4px;
}}
QTabBar::tab {{
    background-color: {COLOR_BG};
    color: {COLOR_MUTED};
    border: 1px solid {COLOR_BORDER};
    padding: 6px 16px;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QTabBar::tab:selected {{
    background-color: {COLOR_PANEL};
    color: {COLOR_TEXT};
    border-bottom: 1px solid {COLOR_PANEL};
}}
QTableWidget {{
    background-color: {COLOR_PANEL};
    border: none;
    gridline-color: {COLOR_BORDER};
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_BLUE_DIM};
}}
QTableWidget::item {{
    padding: 4px 6px;
    border-bottom: 1px solid {COLOR_BORDER};
}}
QHeaderView::section {{
    background-color: {COLOR_BG};
    color: {COLOR_MUTED};
    border: none;
    border-right: 1px solid {COLOR_BORDER};
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 4px 6px;
    font-size: 12px;
}}
QScrollBar:vertical {{
    background-color: {COLOR_BG};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background-color: {COLOR_BORDER};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QCheckBox {{
    color: {COLOR_TEXT};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {COLOR_BORDER};
    border-radius: 3px;
    background-color: {COLOR_INPUT_BG};
}}
QCheckBox::indicator:checked {{
    background-color: {COLOR_BLUE};
    border-color: {COLOR_BLUE};
}}
QSplitter::handle {{
    background-color: {COLOR_BORDER};
    width: 1px;
}}
QStatusBar {{
    background-color: {COLOR_PANEL};
    color: {COLOR_MUTED};
    border-top: 1px solid {COLOR_BORDER};
    font-size: 12px;
}}
"""


# ──────────────────────────────────────────
# MainWindow
# ──────────────────────────────────────────

class MainWindow(QMainWindow):
    TABLE_COLS = ["发布时间", "标题", "相关股票", "股票代码", "摘要"]
    TABLE_WIDTHS = [140, 260, 140, 120, 300]

    def __init__(self):
        super().__init__()
        self.config = ConfigManager.load()
        self._thread: ScraperThread | None = None
        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._countdown_secs = 0
        self._is_running = False
        self._quote_thread: QuoteFetchThread | None = None
        self._quote_timer = QTimer(self)
        self._quote_timer.timeout.connect(self._refresh_quotes)
        self._quote_labels: dict[str, QLabel] = {}  # code → label widget

        self.setWindowTitle("财联社电报监控")
        self.setMinimumSize(1000, 640)
        self.resize(1200, 720)
        self.setStyleSheet(STYLESHEET)

        self._build_ui()
        self._load_config_to_ui()
        self._update_status(False)

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
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_content())
        splitter.setSizes([260, 940])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setStyleSheet(
            f"background-color: {COLOR_PANEL}; "
            f"border-bottom: 1px solid {COLOR_BORDER};"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)

        title = QLabel("财联社电报监控")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {COLOR_TEXT};"
        )
        layout.addWidget(title)
        layout.addStretch()

        self.lbl_status_dot = QLabel("○")
        self.lbl_status_dot.setStyleSheet(f"font-size: 18px; color: {COLOR_MUTED};")
        self.lbl_status_text = QLabel("已停止")
        self.lbl_status_text.setStyleSheet(f"color: {COLOR_MUTED};")
        self.lbl_countdown = QLabel("")
        self.lbl_countdown.setStyleSheet(
            f"color: {COLOR_MUTED}; font-family: monospace; min-width: 80px;"
        )

        layout.addWidget(self.lbl_status_dot)
        layout.addWidget(self.lbl_status_text)
        layout.addSpacing(16)
        layout.addWidget(self.lbl_countdown)
        return bar

    def _build_quotebar(self) -> QWidget:
        from PyQt6.QtWidgets import QScrollArea
        bar = QFrame()
        bar.setFixedHeight(44)
        bar.setStyleSheet(
            f"background-color: {COLOR_PANEL};"
            f"border-bottom: 1px solid {COLOR_BORDER};"
        )
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(10, 0, 10, 0)
        outer.setSpacing(6)

        # 输入框 + 添加按钮
        self.quote_input = QLineEdit()
        self.quote_input.setPlaceholderText("输入股票代码…")
        self.quote_input.setFixedWidth(110)
        self.quote_input.setFixedHeight(28)
        self.quote_input.returnPressed.connect(self._add_watch_code)

        btn_add = QPushButton("+")
        btn_add.setFixedSize(28, 28)
        btn_add.setStyleSheet(
            f"background-color: {COLOR_BLUE_DIM}; color: white;"
            f"border-radius: 4px; font-weight: bold; font-size: 16px;"
        )
        btn_add.clicked.connect(self._add_watch_code)

        outer.addWidget(self.quote_input)
        outer.addWidget(btn_add)

        # 分割线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        outer.addWidget(sep)

        # 可横向滚动的报价区域
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(44)
        scroll.setStyleSheet("background: transparent;")

        self._quote_container = QWidget()
        self._quote_container.setStyleSheet("background: transparent;")
        self._quote_row = QHBoxLayout(self._quote_container)
        self._quote_row.setContentsMargins(0, 0, 0, 0)
        self._quote_row.setSpacing(16)
        self._quote_row.addStretch()
        scroll.setWidget(self._quote_container)
        outer.addWidget(scroll, 1)

        # 刷新按钮
        btn_refresh = QPushButton("↻")
        btn_refresh.setFixedSize(28, 28)
        btn_refresh.setToolTip("立即刷新报价")
        btn_refresh.setStyleSheet(
            f"background-color: transparent; color: {COLOR_MUTED};"
            f"border: 1px solid {COLOR_BORDER}; border-radius: 4px; font-size: 14px;"
        )
        btn_refresh.clicked.connect(self._refresh_quotes)
        outer.addWidget(btn_refresh)

        # 启动定时刷新，并加载已保存的自选股
        for code in self.config.get("watch_codes", []):
            self._add_quote_chip(code)
        if self.config.get("watch_codes"):
            self._refresh_quotes()
        self._quote_timer.start(QUOTE_REFRESH_SECS * 1000)

        return bar

    def _add_watch_code(self):
        code = self.quote_input.text().strip()
        if not code:
            return
        # 校验：6位数字
        if not re.match(r"^\d{6}$", code):
            self.status_bar.showMessage("请输入6位股票代码", 2000)
            return
        codes = self.config.get("watch_codes", [])
        if code in codes:
            self.quote_input.clear()
            return
        codes.append(code)
        self.config["watch_codes"] = codes
        ConfigManager.save(self.config)
        self._add_quote_chip(code)
        self.quote_input.clear()
        self._refresh_quotes()

    def _add_quote_chip(self, code: str):
        """在报价栏添加一个股票 chip（先占位，等数据回来再更新）"""
        chip = QFrame()
        chip.setStyleSheet(
            f"background-color: {COLOR_BG}; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 4px;"
        )
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(6, 0, 4, 0)
        chip_layout.setSpacing(4)

        lbl = QLabel(f"{code}  --")
        lbl.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 12px; border: none;")
        chip_layout.addWidget(lbl)

        btn_close = QPushButton("×")
        btn_close.setFixedSize(16, 16)
        btn_close.setStyleSheet(
            f"background: transparent; color: {COLOR_MUTED}; border: none;"
            f"font-size: 13px; padding: 0;"
        )
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
        # 移除 chip widget
        lbl = self._quote_labels.pop(code, None)
        if lbl:
            chip = lbl.parent()
            self._quote_row.removeWidget(chip)
            chip.deleteLater()

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
                color = COLOR_RED if pct_f >= 0 else COLOR_GREEN  # A股红涨绿跌
                pct_str = f"{sign}{abs(pct_f):.2f}%"
            except Exception:
                color = COLOR_MUTED
                pct_str = "--"
            name = q.get("name", code)
            lbl.setText(f"{name}  ¥{price}  {pct_str}")
            lbl.setStyleSheet(f"color: {color}; font-size: 12px; border: none; font-weight: bold;")

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(
            f"background-color: {COLOR_PANEL}; "
            f"border-right: 1px solid {COLOR_BORDER};"
        )
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # 基本设置
        grp_basic = QGroupBox("⚙  基本设置")
        grp_layout = QVBoxLayout(grp_basic)
        grp_layout.setSpacing(6)

        self.spin_interval = self._make_spinbox(1, 60, self.config["interval_min"], "分钟")
        self.spin_scroll   = self._make_spinbox(1, 20,  self.config["scroll_times"],  "次")
        self.spin_timeout  = self._make_spinbox(5, 120, self.config["wait_timeout"],  "秒")

        grp_layout.addLayout(self._labeled_row("监控频率", self.spin_interval))
        grp_layout.addLayout(self._labeled_row("加载次数", self.spin_scroll))
        grp_layout.addLayout(self._labeled_row("等待超时", self.spin_timeout))
        layout.addWidget(grp_basic)

        # 存储设置
        grp_store = QGroupBox("💾  存储设置")
        grp_store_layout = QVBoxLayout(grp_store)
        self.edit_excel = QLineEdit(self.config["excel_path"])
        self.edit_excel.setPlaceholderText("Excel 保存路径")
        btn_browse = QPushButton("浏览")
        btn_browse.setObjectName("btn_browse")
        btn_browse.setFixedWidth(44)
        btn_browse.clicked.connect(self._browse_excel)
        row = QHBoxLayout()
        row.addWidget(self.edit_excel)
        row.addWidget(btn_browse)
        grp_store_layout.addLayout(row)
        layout.addWidget(grp_store)

        # AI 设置
        grp_ai = QGroupBox("🤖  AI 设置")
        grp_ai_layout = QVBoxLayout(grp_ai)
        grp_ai_layout.setSpacing(6)

        from PyQt6.QtWidgets import QCheckBox
        self.chk_ai     = QCheckBox("启用 AI 分析")
        self.chk_all    = QCheckBox("记录无利好条目")
        self.chk_ai.setChecked(True)
        self.chk_all.setChecked(self.config.get("analyze_all", True))

        detected = ConfigManager.detect_claude_bin()
        self.edit_claude = QLineEdit(self.config.get("claude_bin", "") or detected)
        self.edit_claude.setPlaceholderText("claude 可执行路径")

        lbl_claude = QLabel("Claude 路径")
        lbl_claude.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px;")

        grp_ai_layout.addWidget(self.chk_ai)
        grp_ai_layout.addWidget(self.chk_all)
        grp_ai_layout.addWidget(lbl_claude)
        grp_ai_layout.addWidget(self.edit_claude)
        layout.addWidget(grp_ai)

        # 分割线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        layout.addWidget(sep)

        # 按钮
        self.btn_start = QPushButton("▶  开始监控")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self._start_loop)

        self.btn_stop = QPushButton("■  停  止")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)

        self.btn_once = QPushButton("⚡  立即执行一次")
        self.btn_once.setObjectName("btn_once")
        self.btn_once.clicked.connect(self._run_once)

        self.btn_excel = QPushButton("📂  打开 Excel")
        self.btn_excel.setObjectName("btn_excel")
        self.btn_excel.clicked.connect(self._open_excel)

        for btn in [self.btn_start, self.btn_stop, self.btn_once, self.btn_excel]:
            btn.setFixedHeight(32)
            layout.addWidget(btn)

        layout.addStretch()
        return sidebar

    def _build_content(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

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
        self.edit_excel.setText(self.config.get("excel_path", DEFAULTS["excel_path"]))
        self.chk_all.setChecked(self.config.get("analyze_all", True))
        claude = self.config.get("claude_bin", "") or ConfigManager.detect_claude_bin()
        self.edit_claude.setText(claude)

    def _collect_config(self) -> dict:
        cfg = dict(self.config)
        cfg["interval_min"] = self.spin_interval.value()
        cfg["scroll_times"] = self.spin_scroll.value()
        cfg["wait_timeout"]  = self.spin_timeout.value()
        cfg["excel_path"]    = self.edit_excel.text().strip()
        cfg["analyze_all"]   = self.chk_all.isChecked()
        cfg["claude_bin"]    = self.edit_claude.text().strip()
        cfg["chrome_bin"]    = DEFAULTS["chrome_bin"]
        return cfg

    # ── 状态更新 ──────────────────────────

    def _update_status(self, running: bool):
        self._is_running = running
        if running:
            self.lbl_status_dot.setStyleSheet(f"font-size: 18px; color: {COLOR_GREEN};")
            self.lbl_status_dot.setText("●")
            self.lbl_status_text.setStyleSheet(f"color: {COLOR_GREEN};")
            self.lbl_status_text.setText("运行中")
        else:
            self.lbl_status_dot.setStyleSheet(f"font-size: 18px; color: {COLOR_MUTED};")
            self.lbl_status_dot.setText("○")
            self.lbl_status_text.setStyleSheet(f"color: {COLOR_MUTED};")
            self.lbl_status_text.setText("已停止")
            self.lbl_countdown.setText("")
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_once.setEnabled(not running)

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

    def _on_new_data(self, rows: list):
        # 关闭排序，批量插入后再重新排序（避免插入中途乱序）
        self.table.setSortingEnabled(False)
        for row_dict in rows:
            self._insert_table_row(row_dict)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.tabs.setCurrentIndex(1)

    def _insert_table_row(self, row_dict: dict):
        self.table.insertRow(0)
        stocks_text = row_dict.get("相关股票", "")
        data = [
            row_dict.get("发布时间", ""),
            (row_dict.get("标题", "") or "") + (row_dict.get("内容", "") or "")[:60],
            stocks_text,
            row_dict.get("股票代码", ""),
            row_dict.get("AI分析", ""),
        ]
        has_bullish = "↑" in (stocks_text or "")
        has_bearish = "↓" in (stocks_text or "")
        no_relevant = stocks_text == "无相关股票" or not stocks_text

        for col, val in enumerate(data):
            item = QTableWidgetItem(str(val) if val else "")
            if col == 2:
                if no_relevant:
                    item.setForeground(QColor(COLOR_MUTED))
                elif has_bullish and has_bearish:
                    item.setForeground(QColor("#f59e0b"))   # 混合 → 橙色
                elif has_bullish:
                    item.setForeground(QColor(COLOR_GREEN))
                elif has_bearish:
                    item.setForeground(QColor(COLOR_RED))
            self.table.setItem(0, col, item)
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
        event.accept()


# ──────────────────────────────────────────
# 入口
# ──────────────────────────────────────────

def main():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("财联社监控")

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
