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
    QGroupBox, QLabel, QSpinBox, QAbstractSpinBox, QLineEdit, QPushButton, QTextEdit,
    QTableWidget, QTableWidgetItem, QTabWidget, QFileDialog,
    QHeaderView, QSplitter, QStatusBar, QFrame, QListWidget,
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
    "quote_refresh_secs": 30,  # 报价刷新间隔（秒）
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
            pass


# ──────────────────────────────────────────
# ScraperThread
# ──────────────────────────────────────────

# ──────────────────────────────────────────
# StockListLoader — 后台加载全量股票列表
# ──────────────────────────────────────────

class StockListLoader(QThread):
    loaded = pyqtSignal(list)   # list[dict]: code/name/pinyin

    def run(self):
        try:
            import akshare as ak
            from pypinyin import lazy_pinyin, Style
            df = ak.stock_info_a_code_name()
            result = []
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
    _C_TEXT   = "rgba(235, 238, 245, 230)" # 正文
    _C_MUTED  = "rgba(160, 168, 185, 200)" # 次要文字
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

        # ── 标题栏
        title_bar = QWidget()
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
        for row in rows:
            stocks = row.get("相关股票", "")
            if stocks:
                existing_ids = {r.get("ID") for r in self._news_items}
                if row.get("ID") not in existing_ids:
                    self._news_items.insert(0, row)
        self._news_items = self._news_items[:8]
        self._refresh_news_label()

    def update_quotes(self, quotes: list[dict]):
        self._quote_data = quotes
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
            title  = (item.get("标题", "") or item.get("内容", ""))[:28]
            raw_t  = item.get("发布时间", "")
            t_str  = raw_t[-5:] if raw_t else ""

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
            line = (
                f"<span style='color:{self._C_MUTED};font-size:10px;'>{name}</span>"
                f"<span style='color:{self._C_TEXT};font-size:12px;font-weight:bold;'>"
                f" ¥{price}</span>"
                f"<span style='color:{c};font-size:11px;'> {ps}</span>"
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
        self._quote_thread: QuoteFetchThread | None = None
        self._quote_timer = QTimer(self)
        self._quote_timer.timeout.connect(self._refresh_quotes)
        self._quote_labels: dict[str, QLabel] = {}  # code → label widget
        self._stock_list: list[dict] = []            # 全量股票列表（后台加载）
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

        title = QLabel("财联社监控")
        title.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {COLOR_TEXT};"
        )
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

    def _add_quote_chip(self, code: str):
        """在报价栏添加一个股票 chip（先占位，等数据回来再更新）"""
        chip = QFrame()
        chip.setStyleSheet(
            f"background-color: {COLOR_ELEVATED}; border: 1px solid {COLOR_BORDER};"
            f"border-radius: 8px;"
        )
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(8, 0, 6, 0)
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
        lbl = self._quote_labels.pop(code, None)
        if lbl:
            chip = lbl.parent()
            self._quote_row.removeWidget(chip)
            chip.deleteLater()

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
        # 同步到桌面小组件
        self._desktop_widget.update_quotes(results)

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

        detected = ConfigManager.detect_claude_bin()
        self.edit_claude = QLineEdit(self.config.get("claude_bin", "") or detected)
        self.edit_claude.setPlaceholderText("claude 可执行路径")

        lbl_claude = QLabel("Claude 路径")
        lbl_claude.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px; margin-top: 4px;")

        grp_ai_layout.addSpacing(4)
        grp_ai_layout.addWidget(self.chk_ai)
        grp_ai_layout.addSpacing(8)
        grp_ai_layout.addWidget(self.chk_all)
        grp_ai_layout.addSpacing(4)
        grp_ai_layout.addWidget(lbl_claude)
        grp_ai_layout.addWidget(self.edit_claude)
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
        claude = self.config.get("claude_bin", "") or ConfigManager.detect_claude_bin()
        self.edit_claude.setText(claude)

    def _collect_config(self) -> dict:
        cfg = dict(self.config)
        cfg["interval_min"]        = self.spin_interval.value()
        cfg["scroll_times"]        = self.spin_scroll.value()
        cfg["wait_timeout"]        = self.spin_timeout.value()
        cfg["quote_refresh_secs"]  = self.spin_quote.value()
        cfg["excel_path"]    = self.edit_excel.text().strip()
        cfg["analyze_all"]   = self.chk_all.isChecked()
        cfg["claude_bin"]    = self.edit_claude.text().strip()
        cfg["chrome_bin"]    = DEFAULTS["chrome_bin"]
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
        # 同步到桌面小组件
        self._desktop_widget.update_news(rows)

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
                    item.setForeground(QColor(COLOR_ORANGE))
                elif has_bullish:
                    item.setForeground(QColor(COLOR_RED))   # 利好 → A股红
                elif has_bearish:
                    item.setForeground(QColor(COLOR_GREEN)) # 利空 → A股绿
            self.table.setItem(0, col, item)

        # 最后一列：添加到报价栏按钮（有股票代码时才显示）
        codes_raw = row_dict.get("股票代码", "")
        codes = [c.strip() for c in codes_raw.split("\n") if c.strip()] if codes_raw else []
        if codes:
            btn = QPushButton("+")
            btn.setToolTip("添加到报价栏")
            btn.setFixedSize(26, 26)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLOR_ACCENT};
                    color: #FFFFFF;
                    border: none;
                    border-radius: 7px;
                    font-size: 16px;
                    font-weight: 300;
                    padding: 0;
                }}
                QPushButton:hover {{
                    background-color: #0066DD;
                }}
                QPushButton:pressed {{
                    background-color: #0055BB;
                }}
            """)
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
