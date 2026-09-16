"""
需給ウォッチ 自動レポート生成スクリプト
=========================================

やること:
1. stockscope.app から各銘柄の「大口空売り残高」「個人信用残高」を取得
2. nikkeiyosoku.com から RSI を取得
3. 簡易HTMLレポートを組み立てて保存する
4. (任意) メールやSlack/LINEなどに通知

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
    そのまま取得できる。RSIの時系列テーブルは
    `<table>` の thead に [日付, 終値, 前日比, 前日比％, RSI] という見出しがあり、
    tbody の最初の行(tr)が最新日のデータ。

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
    date: str = ""
    price: int = None
    change_pct: float = None
    volume: int = None
    rsi: float = None
    rsi_date: str = ""
    short_positions: list = field(default_factory=list)   # [{"firm":..., "balance":..., "change":..., "date":...}]
    margin_sell: dict = None   # {"balance":..., "change":..., "date":...}
    margin_buy: dict = None


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


def fetch_short_selling(code: str, page) -> dict:
    """
    stockscope.app の大口空売り残高ページを、レンダリング済みDOMから取得する。
    機関ごとの残高・個人信用残高は毎日更新されるわけではないため、
    直近の掲載範囲(最大20営業日)の中で最新の実データを機関ごとに探す。
    """
    empty = {
        "date": None, "price": None, "change_pct": None, "volume": None,
        "short_positions": [], "margin_sell": None, "margin_buy": None,
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
    for row in rows:
        if margin_sell is None:
            balance, change = _parse_balance_cell(row["sell"])
            if balance is not None:
                margin_sell = {"balance": balance, "change": change, "date": row["date"]}
        if margin_buy is None:
            balance, change = _parse_balance_cell(row["buy"])
            if balance is not None:
                margin_buy = {"balance": balance, "change": change, "date": row["date"]}
        if margin_sell is not None and margin_buy is not None:
            break

    return {
        "date": latest["date"],
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "short_positions": short_positions,
        "margin_sell": margin_sell,
        "margin_buy": margin_buy,
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


def fetch_rsi(code: str) -> dict:
    """
    nikkeiyosoku.com のRSIページ(静的HTML)からRSI時系列テーブルの最新行を取得する。
    """
    url = f"https://nikkeiyosoku.com/stock/technical/rsi/{code}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = None
    for t in soup.find_all("table"):
        head_cells = [th.get_text(strip=True) for th in t.select("thead th")]
        if head_cells == ["日付", "終値", "前日比", "前日比％", "RSI"]:
            table = t
            break

    if table is None:
        print(f"[WARN] {code}: RSIテーブルが見つかりませんでした。サイト構造が変わった可能性があります。")
        return {"rsi": None, "rsi_date": None}

    first_row = table.select_one("tbody tr")
    if first_row is None:
        return {"rsi": None, "rsi_date": None}

    cells = [td.get_text(strip=True) for td in first_row.find_all("td")]
    if len(cells) < 5:
        return {"rsi": None, "rsi_date": None}

    date_str, _close, _change, _change_pct, rsi_str = cells[:5]
    try:
        rsi = float(rsi_str)
    except ValueError:
        rsi = None

    return {"rsi": rsi, "rsi_date": date_str}


def build_snapshot(ticker: dict, short_data: dict) -> StockSnapshot:
    snap = StockSnapshot(code=ticker["code"], name=ticker["name"])
    snap.date = short_data.get("date")
    snap.price = short_data.get("price")
    snap.change_pct = short_data.get("change_pct")
    snap.volume = short_data.get("volume")
    snap.short_positions = short_data.get("short_positions", [])
    snap.margin_sell = short_data.get("margin_sell")
    snap.margin_buy = short_data.get("margin_buy")

    rsi_data = fetch_rsi(ticker["code"])
    snap.rsi = rsi_data.get("rsi")
    snap.rsi_date = rsi_data.get("rsi_date")
    return snap


def _fmt_balance(item):
    if item is None:
        return "データなし"
    change = item.get("change")
    change_str = f"({change:+,})" if change is not None else ""
    return f"{item['balance']:,} {change_str} [{item['date']}時点]"


def render_html_report(snapshots: list) -> str:
    """
    需給ウォッチのHTMLレポートを組み立てる。
    """
    today = datetime.now().strftime("%Y年%m月%d日")
    sections = []
    for s in snapshots:
        if s.short_positions:
            row_htmls = []
            for p in s.short_positions:
                change_str = f"{p['change']:+,}" if p["change"] is not None else "-"
                row_htmls.append(
                    f"<tr><td>{p['firm']}</td><td>{p['balance']:,}</td>"
                    f"<td>{change_str}</td><td>{p['date']}</td></tr>"
                )
            rows = "".join(row_htmls)
        else:
            rows = "<tr><td colspan='4'>大口空売りデータなし</td></tr>"

        price_str = f"{s.price:,}円" if s.price is not None else "取得失敗"
        change_str = f"{s.change_pct:+.2f}%" if s.change_pct is not None else "-"
        rsi_str = f"{s.rsi:.2f} ({s.rsi_date})" if s.rsi is not None else "取得失敗"

        sections.append(f"""
        <section>
          <h2>{s.name} ({s.code})</h2>
          <p>株価: {price_str} ({change_str}) [{s.date or "-"}時点] / RSI: {rsi_str}</p>
          <p>個人信用 売残: {_fmt_balance(s.margin_sell)}</p>
          <p>個人信用 買残: {_fmt_balance(s.margin_buy)}</p>
          <table border="1" cellspacing="0" cellpadding="4">
            <tr><th>機関</th><th>残高</th><th>増減</th><th>日付</th></tr>
            {rows}
          </table>
        </section>
        """)

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><title>需給ウォッチ {today}</title></head>
<body>
<h1>需給ウォッチ {today}</h1>
{''.join(sections)}
</body></html>"""


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
        rsi_str = f"{s.rsi:.2f}" if s.rsi is not None else "取得失敗"
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
