"""
财联社电报抓取 + AI 利好分析程序
每5分钟抓取一次 https://www.cls.cn/telegraph 的电报信息，
通过 Claude Code CLI 分析每条新闻对 A 股的利好情况，保存到 Excel。

依赖:
    pip install selenium webdriver-manager pandas openpyxl schedule
运行方式:
    python3 cls_telegraph.py
"""

import os
import re
import json
import time
import subprocess
import schedule
import pandas as pd
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ──────────────────────────────────────────
# 配置
# ──────────────────────────────────────────
URL          = "https://www.cls.cn/telegraph"
EXCEL_PATH   = Path.home() / "Desktop" / "cls_telegraph.xlsx"
INTERVAL_MIN = 5
SCROLL_TIMES = 3
WAIT_TIMEOUT = 20

# 是否对无明确利好的新闻也写入摘要（True=写"无明确利好"，False=留空）
ANALYZE_ALL  = True

# claude CLI 路径（通常自动检测即可）
CLAUDE_BIN   = "claude"

AI_PROMPT = """你是专业的A股市场分析师。分析以下财联社新闻，判断是否利好A股上市公司。

规则：
1. 只关注A股（沪深两市），不含港股/美股
2. 股票代码必须是6位数字
3. 若无明确A股利好，has_bullish设false，stocks为空数组
4. summary不超过50字

严格返回JSON，不要有任何其他文字：
{"has_bullish":bool,"stocks":[{"code":"6位代码","name":"股票名","reason":"利好理由"}],"summary":"摘要"}"""


# ──────────────────────────────────────────
# AI 分析 — 通过 Claude CLI
# ──────────────────────────────────────────

def analyze_news(title: str, body: str) -> dict | None:
    """调用本地 claude CLI 分析新闻，返回解析后的 dict"""
    news_text = f"{title}{body}".strip()
    if not news_text:
        return None

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # 允许嵌套调用

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", AI_PROMPT, "--output-format", "json"],
            input=news_text,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        if result.returncode != 0:
            print(f"[{now()}] CLI 错误: {result.stderr[:100]}")
            return None

        outer = json.loads(result.stdout)
        raw   = outer.get("result", "")

        # 提取 JSON（去掉可能的 markdown 代码块）
        json_match = re.search(r"\{[\s\S]+\}", raw)
        if not json_match:
            return None

        return json.loads(json_match.group())

    except subprocess.TimeoutExpired:
        print(f"[{now()}] AI 分析超时，跳过")
        return None
    except json.JSONDecodeError as e:
        print(f"[{now()}] JSON 解析失败: {e}")
        return None
    except Exception as e:
        print(f"[{now()}] AI 分析异常: {e}")
        return None


def format_stocks(analysis: dict | None) -> tuple[str, str, str]:
    """格式化分析结果 → (利好股票名, 股票代码, AI分析详情)"""
    if not analysis:
        return "", "", ""

    if not analysis.get("has_bullish") or not analysis.get("stocks"):
        if ANALYZE_ALL:
            return "无明确利好", "", analysis.get("summary", "")
        return "", "", ""

    stocks  = analysis["stocks"]
    names   = "、".join(s.get("name", "") for s in stocks)
    codes   = "、".join(s.get("code", "") for s in stocks)
    lines   = [f"【{s.get('name')}({s.get('code')})】{s.get('reason','')}" for s in stocks]
    detail  = analysis.get("summary", "") + "\n" + "\n".join(lines)

    return names, codes, detail.strip()


# ──────────────────────────────────────────
# 浏览器驱动
# ──────────────────────────────────────────

def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    opts.binary_location = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )


# ──────────────────────────────────────────
# 抓取 & 解析
# ──────────────────────────────────────────

def fetch_items(driver: webdriver.Chrome) -> list[dict]:
    driver.get(URL)
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".telegraph-list,.telg-list,[class*='roll'],[class*='telegraph']")
            )
        )
    except Exception:
        pass
    for _ in range(SCROLL_TIMES):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
    return parse_page(driver)


def parse_page(driver: webdriver.Chrome) -> list[dict]:
    results  = []
    today    = datetime.now().strftime("%Y-%m-%d")
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
        print(f"[{now()}] 未找到电报条目，请检查页面结构")
        return results

    for el in items:
        try:
            text = el.text.strip()
            if not text:
                continue
            lines = text.splitlines()
            if lines and time_pat.match(lines[0].strip()):
                raw_time      = lines[0].strip()
                pub_time      = f"{today} {raw_time}" if re.match(r"^\d{2}:\d{2}", raw_time) else raw_time
                content_lines = lines[1:]
            else:
                pub_time      = ""
                content_lines = lines
            content     = " ".join(l.strip() for l in content_lines if l.strip())
            m           = re.match(r"^(【[^】]+】)(.*)", content, re.DOTALL)
            title       = m.group(1) if m else ""
            body        = m.group(2).strip() if m else content
            uid         = f"{pub_time}_{content[:20]}"
            results.append({
                "ID": uid, "发布时间": pub_time, "标题": title, "内容": body,
                "抓取时间": now(),
                "利好股票": "", "股票代码": "", "AI分析": "", "AI分析时间": "",
            })
        except Exception:
            continue
    return results


# ──────────────────────────────────────────
# AI 批量分析（仅分析未分析的行）
# ──────────────────────────────────────────

def enrich_with_ai(df: pd.DataFrame) -> pd.DataFrame:
    mask    = df["AI分析时间"].isna() | (df["AI分析时间"] == "")
    indices = df.index[mask].tolist()

    if not indices:
        return df

    print(f"[{now()}] 开始 AI 分析，共 {len(indices)} 条...")

    for i, idx in enumerate(indices, 1):
        row      = df.loc[idx]
        title    = str(row.get("标题", "") or "")
        body     = str(row.get("内容", "") or "")
        analysis = analyze_news(title, body)
        names, codes, detail = format_stocks(analysis)

        df.at[idx, "利好股票"]   = names
        df.at[idx, "股票代码"]   = codes
        df.at[idx, "AI分析"]    = detail
        df.at[idx, "AI分析时间"] = now()

        tag = f"✓ {names}" if names and names != "无明确利好" else "- 无明确利好"
        print(f"  [{i}/{len(indices)}] {(title or body)[:35]} → {tag}")
        time.sleep(0.3)

    return df


# ──────────────────────────────────────────
# Excel 存储
# ──────────────────────────────────────────

COLUMNS    = ["ID","发布时间","标题","内容","利好股票","股票代码","AI分析","抓取时间","AI分析时间"]
COL_WIDTHS = {"ID":36,"发布时间":22,"标题":40,"内容":80,
              "利好股票":30,"股票代码":25,"AI分析":80,"抓取时间":22,"AI分析时间":22}


def load_existing(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_excel(path, dtype=str)
        except Exception as e:
            print(f"[{now()}] 读取 Excel 失败，将重建: {e}")
    return pd.DataFrame()


def save_to_excel(df: pd.DataFrame, path: Path, added: int, total: int):
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
    print(f"[{now()}] 新增 {added} 条 → 共 {total} 条 | {path}")


# ──────────────────────────────────────────
# 定时任务
# ──────────────────────────────────────────

def job():
    print(f"\n[{now()}] ── 开始抓取 ──")
    driver = None
    try:
        driver    = build_driver()
        new_items = fetch_items(driver)
        driver.quit()
        driver    = None

        if not new_items:
            print(f"[{now()}] 未获取到数据")
            return

        new_df = pd.DataFrame(new_items)
        old_df = load_existing(EXCEL_PATH)

        if old_df.empty:
            combined = new_df.copy()
        else:
            for col in new_df.columns:
                if col not in old_df.columns:
                    old_df[col] = ""
            combined = pd.concat([old_df, new_df], ignore_index=True)
            combined.drop_duplicates(subset=["ID"], keep="first", inplace=True)

        added    = len(new_df)
        combined = enrich_with_ai(combined)

        if "发布时间" in combined.columns:
            combined.sort_values("发布时间", ascending=False, inplace=True, ignore_index=True)

        save_to_excel(combined, EXCEL_PATH, added, len(combined))

    except Exception as e:
        print(f"[{now()}] 任务异常: {e}")
        import traceback; traceback.print_exc()
    finally:
        if driver:
            driver.quit()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ──────────────────────────────────────────
# 入口
# ──────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  财联社电报抓取 + AI 利好分析程序")
    print(f"  间隔:  每 {INTERVAL_MIN} 分钟")
    print(f"  保存:  {EXCEL_PATH}")
    print("=" * 60 + "\n")

    job()

    schedule.every(INTERVAL_MIN).minutes.do(job)
    print(f"\n[{now()}] 定时任务已启动，按 Ctrl+C 停止\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(10)
    except KeyboardInterrupt:
        print(f"\n[{now()}] 程序已停止")
