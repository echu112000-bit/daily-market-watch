"""
需給ウォッチ 自動レポート生成スクリプト
=========================================

やること:
1. stockscope.app から各銘柄の「大口空売り残高」「個人信用残高」の推移を取得
2. nikkeiyosoku.com から RSI・移動平均乖離・MACD等のテクニカル指標を取得
3. 見た目付きのHTMLレポート(reports/YYYY-MM-DD.html)を組み立てて保存する
4. GitHub Pagesで公開したレポートへのリンクをDiscordに通知する

■ サイトの実際の構造(2026-09-16 に確認済み)
  - stockscope.app: Next.js + antd の Table でクライアント側(JS)描画される。
    単純な requests + BeautifulSoup では表の中身が取得できないため、
    Playwright でヘッドレスブラウザを操作してレンダリング後のDOMから抽出する。
    ヘッダーと本体が別々の<table>に分かれており、日付ごとの行は
    `.ant-table-tbody tr` (document全体から探す)で取得できる。列は
    [日付, 株価, 出来高, 機関1, 機関2, ..., 機関N, 全増減, 売(個人信用), 買(個人信用)]。
    機関ごとの残高セルはデータがある日だけ「残高\n増減」の2行、ない日は「-」。
    機関の一覧・数は銘柄ごとに異なるため、ヘッダー行から動的に取得する。
  - nikkeiyosoku.com: サーバー側で描画された静的HTMLなので requests + BeautifulSoup で
    そのまま取得できる。`/stock/technical/{code}/` (テクニカル分析タブ) に
    移動平均乖離(5/25/75/200日)、RSI/MACD/モメンタム/サイコロジカル等の指標と
    判定(買/売/無/強/弱/通)、売り・中立・買いシグナルの集計が1ページにまとまっている
    (table.tb-teck の tbody tr = 指標ごとの行)。
    発行済株式数は公開されていないが、`/stock/{code}/` (株価タブ)の
    table.tb-st-font に「時価総額」(例: 5985億4500万)があるので、
    時価総額 ÷ 株価 で概算し、空売り比率の算出に使う。

  ※ 個別のニュース背景(株価変動の理由)は、これらのサイトから機械的に
    取得できないため、このレポートには含めていない。発行済株式数も実際の
    開示値ではなく時価総額からの逆算(概算)である点に注意。

■ セットアップ
  pip install requests beautifulsoup4 playwright
  playwright install chromium

■ 注意
  各サイトの利用規約を確認し、アクセス頻度(1日1回程度が無難)や
  robots.txt の内容に従うこと。サイトのHTML構造は将来変わる可能性があるため、
  取得に失敗した場合はブラウザの「検証」機能で構造を再確認すること。
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dataclasses import dataclass, field

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ---------------------------------------------------------------------------
# 監視したい銘柄一覧(ここに追加していけば対象を増やせる)
# ---------------------------------------------------------------------------
TICKERS = [
    {"code": "4385", "name": "メルカリ"},
    {"code": "6777", "name": "santec Holdings"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalResearchBot/1.0)"
}

# レポートをGitHub Pagesで公開する際のベースURL。
# reports/YYYY-MM-DD.html を push すると https://<user>.github.io/<repo>/reports/YYYY-MM-DD.html で閲覧できる。
REPORT_BASE_URL = os.environ.get(
    "REPORT_BASE_URL",
    "https://echu112000-bit.github.io/daily-market-watch/reports",
)

# stockscope.app のレンダリング後DOMから表データを抜き出すJS。
# 仮想テーブルの計測用の非表示行(日付列が日付形式でない)は除外する。
EXTRACT_TABLE_JS = """
() => {
  const table = document.querySelector('table');
  if (!table) return null;
  const headerRows = table.querySelectorAll('.ant-table-thead tr');
  if (headerRows.length < 2) return null;
  const subHeaderCells = Array.from(headerRows[1].children).map(th => th.textContent.trim());
  const numFirms = subHeaderCells.length - 2; // 末尾2列は 売/買(個人信用)
  const firmNames = subHeaderCells.slice(0, numFirms);

  // 表はヘッダー用とスクロール本体用に別々の<table>に分かれているため、
  // 本体側は document 全体から .ant-table-tbody を直接探す(祖先を .ant-table-body に限定しない)。
  const tbody = document.querySelector('.ant-table-tbody');
  if (!tbody) return null;
  const trs = Array.from(tbody.querySelectorAll('tr')).filter(tr => {
    const first = tr.children[0];
    return first && /^\\d{4}-\\d{2}-\\d{2}/.test(first.innerText.trim());
  });

  const rows = trs.map(tr => {
    const cells = Array.from(tr.children).map(td => td.innerText.trim());
    return {
      date: cells[0],
      price: cells[1],
      volume: cells[2],
      firms: cells.slice(3, 3 + numFirms),
      zenzougen: cells[3 + numFirms],
      sell: cells[3 + numFirms + 1],
      buy: cells[3 + numFirms + 2],
    };
  });

  return { firmNames, rows };
}
"""


@dataclass
class StockSnapshot:
    code: str
    name: str
    market: str = None
    date: str = ""
    price: int = None
    change_pct: float = None
    price_change: float = None
    volume: int = None
    ma_deviation: dict = field(default_factory=dict)          # {"5日": "-5.31%", ...}
    indicators: list = field(default_factory=list)            # [{"name","value","judge","judge_class"}]
    summary_counts: dict = field(default_factory=dict)        # {"sell":0,"neutral":4,"buy":3}
    short_positions: list = field(default_factory=list)       # [{"firm","balance","change","date"}]
    short_positions_total: int = None
    shares_outstanding: int = None                            # 時価総額÷株価から逆算した概算値
    short_ratio: float = None                                 # 空売り合計 / 発行済株式数 (%)
    daily_change_history: list = field(default_factory=list)  # [{"date","price","change_pct","zenzougen"}]
    margin_sell: dict = None                                  # {"balance","change","date"}
    margin_buy: dict = None
    margin_history: list = field(default_factory=list)        # [{"date","sell_balance","sell_change","buy_balance","buy_change","ratio"}]


def _parse_price_cell(text: str):
    """ "3,621円\\n-6.36%" -> (3621, -6.36) """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None, None
    price = None
    try:
        price = int(lines[0].replace(",", "").replace("円", ""))
    except ValueError:
        price = None
    change_pct = None
    if len(lines) > 1:
        try:
            change_pct = float(lines[1].replace("%", "").replace("+", ""))
        except ValueError:
            change_pct = None
    return price, change_pct


def _parse_balance_cell(text: str):
    """ "1,169,783\\n+198,898" -> (1169783, 198898) / "-" -> (None, None) """
    text = text.strip()
    if text in ("", "-"):
        return None, None

    def to_int(s):
        s = s.replace(",", "").replace("+", "")
        try:
            return int(s)
        except ValueError:
            return None

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    balance = to_int(lines[0])
    change = to_int(lines[1]) if len(lines) > 1 else None
    return balance, change


def _parse_int_cell(text: str):
    text = text.strip()
    if text in ("", "-"):
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def _parse_signed_int(text: str):
    text = text.strip()
    if text in ("", "-"):
        return None
    try:
        return int(text.replace(",", "").replace("+", ""))
    except ValueError:
        return None


def fetch_short_selling(code: str, page) -> dict:
    """
    stockscope.app の大口空売り残高ページを、レンダリング済みDOMから取得する。
    機関ごとの残高・個人信用残高は毎日更新されるわけではないため、
    直近の掲載範囲(最大20営業日)の中で最新の実データを機関ごとに探す。
    あわせて、空売り全体の日次増減・個人信用残高の推移も時系列で拾う。
    """
    empty = {
        "date": None, "price": None, "change_pct": None, "volume": None,
        "short_positions": [], "margin_sell": None, "margin_buy": None,
        "daily_change_history": [], "margin_history": [],
    }

    url = f"https://stockscope.app/outstanding-short-selling-balances/{code}"

    # サイト側の接続が瞬断されることがあるため、軽くリトライする
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            break
        except Exception as exc:
            if attempt == 2:
                print(f"[WARN] {code}: ページの取得に失敗しました({exc})。")
                return empty
            page.wait_for_timeout(3000)

    try:
        page.wait_for_selector(".ant-table-tbody tr", state="attached", timeout=20000)
    except PlaywrightTimeoutError:
        print(f"[WARN] {code}: テーブルの読み込みがタイムアウトしました(サイト構造の変更/要ログインの可能性)。")
        return empty
    page.wait_for_timeout(500)  # antdの行描画が落ち着くのを待つ

    raw = page.evaluate(EXTRACT_TABLE_JS)
    if not raw or not raw["rows"]:
        print(f"[WARN] {code}: データ行が見つかりませんでした。")
        return empty

    rows = raw["rows"]           # 新しい日付が先頭
    firm_names = raw["firmNames"]

    latest = rows[0]
    price, change_pct = _parse_price_cell(latest["price"])
    volume = _parse_int_cell(latest["volume"])

    short_positions = []
    for i, firm in enumerate(firm_names):
        for row in rows:
            balance, change = _parse_balance_cell(row["firms"][i])
            if balance is not None:
                short_positions.append({
                    "firm": firm, "balance": balance, "change": change, "date": row["date"],
                })
                break

    margin_sell = None
    margin_buy = None
    margin_history = []
    for row in rows:
        sell_balance, sell_change = _parse_balance_cell(row["sell"])
        buy_balance, buy_change = _parse_balance_cell(row["buy"])
        if margin_sell is None and sell_balance is not None:
            margin_sell = {"balance": sell_balance, "change": sell_change, "date": row["date"]}
        if margin_buy is None and buy_balance is not None:
            margin_buy = {"balance": buy_balance, "change": buy_change, "date": row["date"]}
        if sell_balance is not None or buy_balance is not None:
            ratio = (buy_balance / sell_balance) if sell_balance else None
            margin_history.append({
                "date": row["date"],
                "sell_balance": sell_balance, "sell_change": sell_change,
                "buy_balance": buy_balance, "buy_change": buy_change,
                "ratio": ratio,
            })
    margin_history.reverse()  # 古い→新しい順

    daily_change_history = []
    for row in rows:
        zenzougen = _parse_signed_int(row["zenzougen"])
        if not zenzougen:  # None または 0(変化なし)は除外
            continue
        row_price, row_change_pct = _parse_price_cell(row["price"])
        daily_change_history.append({
            "date": row["date"], "price": row_price, "change_pct": row_change_pct, "zenzougen": zenzougen,
        })
    daily_change_history.reverse()  # 古い→新しい順

    return {
        "date": latest["date"],
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "short_positions": short_positions,
        "margin_sell": margin_sell,
        "margin_buy": margin_buy,
        "margin_history": margin_history,
        "daily_change_history": daily_change_history,
    }


def fetch_all_short_selling(codes: list) -> dict:
    """複数銘柄分をブラウザ1つ使い回しで取得する。"""
    if not PLAYWRIGHT_AVAILABLE:
        print("[ERROR] playwright がインストールされていません。"
              "`pip install playwright` と `playwright install chromium` を実行してください。")
        return {code: {} for code in codes}

    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        for code in codes:
            try:
                result[code] = fetch_short_selling(code, page)
            except PlaywrightTimeoutError:
                print(f"[WARN] {code}: ページの読み込みがタイムアウトしました。")
                result[code] = {}
        browser.close()
    return result


def fetch_technical_indicators(code: str) -> dict:
    """
    nikkeiyosoku.com のテクニカル分析ページ(静的HTML)から、
    移動平均乖離・RSI/MACD等の指標・市場区分を取得する。
    """
    empty = {
        "market": None, "price_change": None,
        "ma_deviation": {}, "indicators": [], "summary_counts": {},
    }

    url = f"https://nikkeiyosoku.com/stock/technical/{code}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    result = dict(empty)
    result["ma_deviation"] = {}
    result["indicators"] = []
    result["summary_counts"] = {}

    # 前日比(円)。%はstockscope側の値を使うのでここでは絶対値のみ利用する。
    price_texts = soup.select(".stockprice-text")
    if len(price_texts) >= 2:
        change_span = price_texts[1].find("span")
        if change_span:
            m = re.match(r"([+-][\d,.]+)\([+-]?[\d.]+%\)", change_span.get_text(strip=True))
            if m:
                try:
                    result["price_change"] = float(m.group(1).replace(",", ""))
                except ValueError:
                    result["price_change"] = None

    market_span = soup.select_one(
        ".st-h1-market .listed-prime, .st-h1-market .listed-standard, .st-h1-market .listed-growth"
    )
    if market_span:
        result["market"] = "東証" + market_span.get_text(strip=True)

    ma_list = soup.select_one("ul.fore-list-arrow")
    if ma_list:
        periods = ["5日", "25日", "75日", "200日"]
        for period, li in zip(periods, ma_list.find_all("li")):
            value_tag = li.find("div", class_=lambda c: c in ("fall", "rise"))
            if value_tag:
                result["ma_deviation"][period] = value_tag.get_text(strip=True)

    table = soup.select_one("table.tb-teck")
    if table:
        for tr in table.select("tbody tr"):
            th = tr.find("th")
            tds = tr.find_all("td")
            if not th or len(tds) < 2:
                continue
            name_link = th.find("a")
            if not name_link or not name_link.contents:
                continue
            name = str(name_link.contents[0]).replace("\xa0", " ").strip()
            value_text = tds[0].get_text(strip=True)
            judge_span = tds[1].find("span")
            judge = judge_span.get_text(strip=True) if judge_span else None
            judge_class = judge_span.get("class", [None])[0] if judge_span else None
            result["indicators"].append({
                "name": name, "value": value_text or None, "judge": judge, "judge_class": judge_class,
            })

    fall = soup.select_one(".number-fall")
    neutral = soup.select_one(".number-neutral")
    rise = soup.select_one(".number-rise")
    if fall and neutral and rise:
        try:
            result["summary_counts"] = {
                "sell": int(fall.get_text(strip=True)),
                "neutral": int(neutral.get_text(strip=True)),
                "buy": int(rise.get_text(strip=True)),
            }
        except ValueError:
            pass

    return result


def _parse_japanese_amount(text: str):
    """ "5985億4500万" -> 598545000000 (円) """
    text = text.strip()
    m_oku = re.search(r"([\d,]+)億", text)
    m_man = re.search(r"([\d,]+)万", text)
    if not m_oku and not m_man:
        return None
    total = 0
    if m_oku:
        total += int(m_oku.group(1).replace(",", "")) * 10**8
    if m_man:
        total += int(m_man.group(1).replace(",", "")) * 10**4
    return total


def fetch_market_cap(code: str):
    """
    nikkeiyosoku.com の株価ページ(静的HTML)から時価総額(円)を取得する。
    発行済株式数が非公開のため、空売り比率の概算に使う。
    """
    url = f"https://nikkeiyosoku.com/stock/{code}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for tr in soup.select("table.tb-st-font tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2 and tds[0].get_text(strip=True) == "時価総額":
            return _parse_japanese_amount(tds[1].get_text(strip=True))
    return None


def build_snapshot(ticker: dict, short_data: dict) -> StockSnapshot:
    snap = StockSnapshot(code=ticker["code"], name=ticker["name"])
    snap.date = short_data.get("date")
    snap.price = short_data.get("price")
    snap.change_pct = short_data.get("change_pct")
    snap.volume = short_data.get("volume")
    snap.short_positions = sorted(
        short_data.get("short_positions", []), key=lambda p: p["balance"], reverse=True
    )
    snap.short_positions_total = (
        sum(p["balance"] for p in snap.short_positions) if snap.short_positions else None
    )
    snap.daily_change_history = short_data.get("daily_change_history", [])
    snap.margin_sell = short_data.get("margin_sell")
    snap.margin_buy = short_data.get("margin_buy")
    snap.margin_history = short_data.get("margin_history", [])

    tech_data = fetch_technical_indicators(ticker["code"])
    snap.market = tech_data.get("market")
    snap.price_change = tech_data.get("price_change")
    snap.ma_deviation = tech_data.get("ma_deviation", {})
    snap.indicators = tech_data.get("indicators", [])
    snap.summary_counts = tech_data.get("summary_counts", {})

    market_cap = fetch_market_cap(ticker["code"])
    if market_cap and snap.price:
        snap.shares_outstanding = round(market_cap / snap.price)
    if snap.short_positions_total and snap.shares_outstanding:
        snap.short_ratio = snap.short_positions_total / snap.shares_outstanding * 100

    return snap


def _fmt_balance(item):
    if item is None:
        return "データなし"
    change = item.get("change")
    change_str = f"({change:+,})" if change is not None else ""
    return f"{item['balance']:,} {change_str} [{item['date']}時点]"


def _judge_css_class(judge_class):
    if judge_class == "buy":
        return "buy"
    if judge_class == "sell":
        return "sell"
    return "neutral"


REPORT_CSS = """
  :root {
    --bg: #f6f5f2;
    --panel: #ffffff;
    --ink: #1c1b19;
    --ink-soft: #5c5850;
    --line: #e4e1da;
    --accent: #2b5d5a;
    --buy: #1a7f4b;
    --buy-bg: #e5f5ec;
    --sell: #b5322b;
    --sell-bg: #fbeae9;
    --neutral: #8a6d1f;
    --neutral-bg: #f6f0dd;
    --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #15181a; --panel: #1d2124; --ink: #ece9e3; --ink-soft: #a3a099;
      --line: #33383b; --accent: #6fb8ae; --buy: #4fd18b; --buy-bg: #16332480;
      --sell: #ff8a80; --sell-bg: #3a191680; --neutral: #e0c66b; --neutral-bg: #332d1480;
    }
  }
  :root[data-theme="dark"] {
    --bg: #15181a; --panel: #1d2124; --ink: #ece9e3; --ink-soft: #a3a099;
    --line: #33383b; --accent: #6fb8ae; --buy: #4fd18b; --buy-bg: #16332480;
    --sell: #ff8a80; --sell-bg: #3a191680; --neutral: #e0c66b; --neutral-bg: #332d1480;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, "Hiragino Sans", "Yu Gothic", sans-serif;
    line-height: 1.6; padding: 24px 16px 60px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }
  header.page-head { margin-bottom: 28px; }
  header.page-head .date { font-size: 13px; color: var(--ink-soft); letter-spacing: 0.02em; }
  header.page-head h1 { font-size: 26px; margin: 4px 0 0; font-weight: 700; }
  section.stock {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 20px 20px 24px; margin-bottom: 24px;
  }
  .stock-head {
    display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 8px 16px; border-bottom: 1px solid var(--line);
    padding-bottom: 14px; margin-bottom: 18px;
  }
  .stock-head h2 { font-size: 20px; margin: 0; }
  .stock-head .code { color: var(--ink-soft); font-family: var(--mono); font-size: 13px; }
  .price-line { font-family: var(--mono); font-size: 15px; }
  .price-line .px { font-size: 20px; font-weight: 700; margin-right: 8px; }
  .up { color: var(--buy); }
  .down { color: var(--sell); }
  .sell-text { color: var(--sell); font-weight: 700; }
  .buy-text { color: var(--buy); font-weight: 700; }
  h3.sub {
    font-size: 13px; color: var(--ink-soft); margin: 22px 0 10px;
    font-weight: 600; letter-spacing: 0.01em;
  }
  h3.sub:first-of-type { margin-top: 0; }
  .chip-row { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip {
    border-radius: 8px; padding: 8px 12px; font-size: 13px; display: flex;
    flex-direction: column; gap: 2px; min-width: 108px; border: 1px solid var(--line);
  }
  .chip .label { color: var(--ink-soft); font-size: 11px; }
  .chip .val { font-family: var(--mono); font-weight: 700; font-size: 15px; }
  .chip.buy { background: var(--buy-bg); border-color: transparent; }
  .chip.buy .val { color: var(--buy); }
  .chip.sell { background: var(--sell-bg); border-color: transparent; }
  .chip.sell .val { color: var(--sell); }
  .chip.neutral { background: var(--neutral-bg); border-color: transparent; }
  .chip.neutral .val { color: var(--neutral); }
  .table-scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; min-width: 420px; }
  th, td { padding: 8px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--line); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--ink-soft); font-weight: 600; font-size: 12px; background: color-mix(in srgb, var(--line) 35%, transparent); }
  tr:last-child td { border-bottom: none; }
  td.num { font-family: var(--mono); }
  .tag { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 5px; }
  .tag.buy { background: var(--buy-bg); color: var(--buy); }
  .tag.sell { background: var(--sell-bg); color: var(--sell); }
  .tag.neutral { background: var(--neutral-bg); color: var(--neutral); }
  .note { font-size: 12.5px; color: var(--ink-soft); margin-top: 10px; }
  .summary-box {
    border-left: 3px solid var(--accent); padding: 10px 14px;
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    border-radius: 0 6px 6px 0; font-size: 13.5px; margin-top: 18px;
  }
  footer { max-width: 880px; margin: 20px auto 0; font-size: 11.5px; color: var(--ink-soft); text-align: center; }
"""


def _render_stock_section(s: StockSnapshot) -> str:
    up_down = "up" if (s.change_pct or 0) >= 0 else "down"
    price_change_str = ""
    if s.price_change is not None and s.change_pct is not None:
        price_change_str = f"{s.price_change:+.0f} ({s.change_pct:+.2f}%)"
    elif s.change_pct is not None:
        price_change_str = f"{s.change_pct:+.2f}%"
    price_str = f"{s.price:,}円" if s.price is not None else "取得失敗"
    code_line = f"{s.code}" + (f" ・ {s.market}" if s.market else "")

    # --- テクニカル(チップ) ---
    chips = [ind for ind in s.indicators if ind.get("value")]
    chip_html = "".join(
        f'<div class="chip {_judge_css_class(ind["judge_class"])}">'
        f'<span class="label">{ind["name"]}</span><span class="val">{ind["value"]}</span></div>'
        for ind in chips
    ) or '<p class="note">テクニカル指標を取得できませんでした。</p>'

    ma_note = " / ".join(f"{period} {val}" for period, val in s.ma_deviation.items() if val)
    ma_note_html = f'<p class="note">移動平均乖離: {ma_note}</p>' if ma_note else ""

    # --- 大口空売り残高 ---
    if s.short_positions:
        short_rows = "".join(
            f"<tr><td>{p['firm']}</td><td class='num'>{p['balance']:,}</td>"
            f"<td class='num'>{p['date']}</td></tr>"
            for p in s.short_positions
        )
        total_row = (
            f"<tr><td><strong>合計(概算)</strong></td>"
            f"<td class='num'><strong>{s.short_positions_total:,}株</strong></td><td class='num'>—</td></tr>"
        )
        if s.short_ratio is not None:
            oku_shares = s.shares_outstanding / 10**8
            ratio_note = (
                f'<p class="note">発行済株式数に対する比率(概算): <strong>約{s.short_ratio:.1f}%</strong>'
                f"(時価総額÷株価から逆算した約{oku_shares:.2f}億株ベース)</p>"
            )
        else:
            ratio_note = ""
        short_table = f"""
        <h3 class="sub">大口空売り残高(機関投資家・最新判明分)</h3>
        <div class="table-scroll">
          <table>
            <tr><th>機関</th><th>残高(株)</th><th>更新日</th></tr>
            {short_rows}{total_row}
          </table>
        </div>
        {ratio_note}
        """
    else:
        short_table = '<h3 class="sub">大口空売り残高</h3><p class="note">大口空売りデータなし</p>'

    # --- 空売り残の日次増減 ---
    if s.daily_change_history:
        change_rows = []
        for h in s.daily_change_history:
            price_disp = f"{h['price']:,}円" if h["price"] is not None else "-"
            pct_cls = "up" if (h["change_pct"] or 0) >= 0 else "down"
            pct_disp = f"{h['change_pct']:+.2f}%" if h["change_pct"] is not None else "-"
            zz_cls = "sell-text" if h["zenzougen"] > 0 else "buy-text"
            change_rows.append(
                f"<tr><td>{h['date']}</td><td class='num'>{price_disp}</td>"
                f"<td class='num {pct_cls}'>{pct_disp}</td>"
                f"<td class='num {zz_cls}'>{h['zenzougen']:+,}</td></tr>"
            )
        change_table = f"""
        <h3 class="sub">空売り残の変動経緯(全機関・日次純増減)</h3>
        <div class="table-scroll">
          <table>
            <tr><th>日付</th><th>株価</th><th>前日比</th><th>空売り全体増減</th></tr>
            {''.join(change_rows)}
          </table>
        </div>
        """
    else:
        change_table = ""

    # --- 個人信用残高(週次) ---
    if s.margin_history:
        has_ratio = any(m["ratio"] for m in s.margin_history)
        header_cols = "<th>日付</th><th>売り残</th><th>買い残</th>" + ("<th>倍率</th>" if has_ratio else "")
        margin_rows = []
        for m in s.margin_history:
            sell_disp = f"{m['sell_balance']:,}" if m["sell_balance"] is not None else "-"
            buy_disp = f"{m['buy_balance']:,}" if m["buy_balance"] is not None else "-"
            if has_ratio:
                ratio_cell = f"<td class='num'>{m['ratio']:.2f}倍</td>" if m["ratio"] else "<td class='num'>-</td>"
            else:
                ratio_cell = ""
            margin_rows.append(
                f"<tr><td>{m['date']}</td><td class='num'>{sell_disp}</td>"
                f"<td class='num'>{buy_disp}</td>{ratio_cell}</tr>"
            )
        margin_table = f"""
        <h3 class="sub">個人信用残高(週次)</h3>
        <div class="table-scroll">
          <table>
            <tr>{header_cols}</tr>
            {''.join(margin_rows)}
          </table>
        </div>
        """
    else:
        margin_table = ""

    # --- サマリー(機械的に算出できる事実のみ。ニュース等の定性コメントは含めない) ---
    summary_tags = []
    counts = s.summary_counts
    if counts.get("buy") is not None:
        summary_tags.append(f'<span class="tag buy">買いシグナル {counts["buy"]}</span>')
    if counts.get("neutral") is not None:
        summary_tags.append(f'<span class="tag neutral">中立 {counts["neutral"]}</span>')
    if counts.get("sell") is not None:
        summary_tags.append(f'<span class="tag sell">売りシグナル {counts["sell"]}</span>')

    summary_facts = []
    if s.short_positions_total:
        ratio_text = f" / 発行済株式数比{s.short_ratio:.1f}%" if s.short_ratio is not None else ""
        summary_facts.append(
            f"大口空売り残高 合計(概算): {s.short_positions_total:,}株"
            f"({len(s.short_positions)}社が最新判明分){ratio_text}"
        )
    if s.margin_history:
        latest_margin = s.margin_history[-1]
        ratio_text = f" / 倍率 {latest_margin['ratio']:.2f}倍" if latest_margin["ratio"] else ""
        summary_facts.append(
            f"個人信用(直近 {latest_margin['date']}時点): "
            f"売り残 {latest_margin['sell_balance']:,} / 買い残 {latest_margin['buy_balance']:,}{ratio_text}"
        )

    summary_box = ""
    if summary_tags or summary_facts:
        summary_box = (
            '<div class="summary-box">'
            + "".join(summary_tags)
            + ("<br><br>" if summary_tags and summary_facts else "")
            + "<br>".join(summary_facts)
            + "</div>"
        )

    return f"""
    <section class="stock">
      <div class="stock-head">
        <div>
          <h2>{s.name}</h2>
          <span class="code">{code_line}</span>
        </div>
        <div class="price-line">
          <span class="px">{price_str}</span>
          <span class="{up_down}">{price_change_str}</span>
        </div>
      </div>

      <h3 class="sub">テクニカル</h3>
      <div class="chip-row">{chip_html}</div>
      {ma_note_html}

      {short_table}
      {change_table}
      {margin_table}
      {summary_box}
    </section>
    """


def render_html_report(snapshots: list) -> str:
    """
    需給ウォッチのHTMLレポートを組み立てる。
    """
    today = datetime.now().strftime("%Y年%m月%d日")
    sections = "".join(_render_stock_section(s) for s in snapshots)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>需給ウォッチ — {today}</title>
<style>{REPORT_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="page-head">
    <div class="date">{today} 終値ベース</div>
    <h1>需給ウォッチ</h1>
  </header>
  {sections}
  <footer>
    データ出典:株ビジョン(stockscope.app)、投資の森(nikkeiyosoku.com)の公開情報をもとに自動生成した参考資料です。投資判断はご自身でお願いします。
  </footer>
</div>
</body>
</html>"""


def render_discord_message(snapshots: list, report_url: str) -> str:
    """
    Discord Webhook向けの短いサマリー + 詳細レポートへのリンクを組み立てる。
    詳細(機関別の残高など)はリンク先のHTMLレポートで確認する想定。
    """
    today = datetime.now().strftime("%Y年%m月%d日")
    lines = [f"**需給ウォッチ {today}**", ""]
    for s in snapshots:
        price_str = f"{s.price:,}円" if s.price is not None else "取得失敗"
        change_str = f"{s.change_pct:+.2f}%" if s.change_pct is not None else "-"
        rsi = next((i["value"] for i in s.indicators if i["name"].startswith("RSI")), None)
        rsi_str = rsi if rsi else "取得失敗"
        lines.append(f"{s.name} ({s.code}): {price_str} ({change_str}) / RSI {rsi_str}")

    lines.append("")
    lines.append(f"詳細レポート: {report_url}")
    return "\n".join(lines)


def send_discord_notification(webhook_url: str, snapshots: list, report_url: str):
    content = render_discord_message(snapshots, report_url)
    resp = requests.post(webhook_url, json={"content": content}, timeout=15)
    if resp.status_code >= 300:
        print(f"[WARN] Discord通知に失敗しました: {resp.status_code} {resp.text}")


def notify(report_html: str, snapshots: list):
    """
    レポートを reports/YYYY-MM-DD.html として保存する(GitHub Pagesで公開する前提)。
    DISCORD_WEBHOOK_URL が設定されていれば、要点とレポートへのリンクをDiscordに通知する。
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    os.makedirs("reports", exist_ok=True)
    output_path = os.path.join("reports", f"{date_str}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    print(f"レポートを {output_path} に保存しました。")

    report_url = f"{REPORT_BASE_URL}/{date_str}.html"

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook_url:
        send_discord_notification(webhook_url, snapshots, report_url)
    else:
        print("[INFO] DISCORD_WEBHOOK_URL が未設定のため、Discord通知はスキップしました。")


def main():
    codes = [t["code"] for t in TICKERS]
    short_data_by_code = fetch_all_short_selling(codes)

    snapshots = [
        build_snapshot(t, short_data_by_code.get(t["code"], {}))
        for t in TICKERS
    ]
    report_html = render_html_report(snapshots)
    notify(report_html, snapshots)


if __name__ == "__main__":
    main()
