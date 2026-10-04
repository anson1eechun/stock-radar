"""證交所、櫃買中心、期交所資料收集。"""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta

from ..db import dumps, upsert
from ..http import get_json
from ..timeutil import TW, iso, roc_to_date, roc_ym, today_tw

log = logging.getLogger(__name__)

STOCK_RE = re.compile(r"^[1-9]\d{3}$")
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"


def num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _table(data: dict, title_part: str) -> dict | None:
    for t in data.get("tables") or []:
        if title_part in (t.get("title") or ""):
            return t
    return None


def _tpex_table(data: dict, d: date) -> dict | None:
    """櫃買在休市日會回傳前一個交易日的資料，所以要核對日期。"""
    tables = data.get("tables") or []
    if not tables or not tables[0].get("data"):
        return None
    t = tables[0]
    if t.get("date") and roc_to_date(t["date"]) != d:
        return None
    return t


# ---------- 每日行情 ----------

def fetch_twse_quotes(d: date):
    data = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json")
    if data.get("stat") != "OK" or data.get("date") != f"{d:%Y%m%d}":
        return None
    t = _table(data, "每日收盤行情")
    if not t or not t.get("data"):
        return None
    f = {name: i for i, name in enumerate(t["fields"])}
    quotes = []
    for r in t["data"]:
        code = r[f["證券代號"]].strip()
        close = num(r[f["收盤價"]])
        if not STOCK_RE.match(code) or close is None:
            continue
        quotes.append({
            "symbol": code, "name": r[f["證券名稱"]].strip(), "date": d.isoformat(),
            "open": num(r[f["開盤價"]]), "high": num(r[f["最高價"]]), "low": num(r[f["最低價"]]),
            "close": close, "volume": num(r[f["成交股數"]]), "value": num(r[f["成交金額"]]), "market": "TWSE"})
    idx = _table(data, "價格指數(臺灣證券交易所)")
    indices = [{"name": r[0].strip(), "date": d.isoformat(), "close": num(r[1])}
               for r in (idx["data"] if idx else []) if num(r[1]) is not None]
    return quotes, indices


def fetch_tpex_quotes(d: date):
    data = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={d:%Y/%m/%d}&type=EW&response=json")
    t = _tpex_table(data, d)
    if not t:
        return None
    f = {name.strip(): i for i, name in enumerate(t["fields"])}
    quotes = []
    for r in t["data"]:
        code = r[f["代號"]].strip()
        close = num(r[f["收盤"]])
        if not STOCK_RE.match(code) or close is None:
            continue
        quotes.append({
            "symbol": code, "name": r[f["名稱"]].strip(), "date": d.isoformat(),
            "open": num(r[f["開盤"]]), "high": num(r[f["最高"]]), "low": num(r[f["最低"]]),
            "close": close, "volume": num(r[f["成交股數"]]), "value": num(r[f["成交金額(元)"]]), "market": "TPEX"})
    return quotes, []


# ---------- 三大法人 ----------

def fetch_twse_inst(d: date):
    data = get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALL&response=json")
    if data.get("stat") != "OK" or data.get("date") != f"{d:%Y%m%d}":
        return None
    f = {n: i for i, n in enumerate(data["fields"])}
    rows = []
    for r in data["data"]:
        code = r[0].strip()
        if not STOCK_RE.match(code):
            continue
        foreign = (num(r[f["外陸資買賣超股數(不含外資自營商)"]]) or 0) + (num(r[f["外資自營商買賣超股數"]]) or 0)
        rows.append({"symbol": code, "date": d.isoformat(), "foreign_net": foreign,
                     "trust_net": num(r[f["投信買賣超股數"]]), "dealer_net": num(r[f["自營商買賣超股數"]]),
                     "total_net": num(r[f["三大法人買賣超股數"]])})
    return rows


def fetch_tpex_inst(d: date):
    data = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json")
    t = _tpex_table(data, d)
    if not t:
        return None
    # 欄位依序：外資(不含自營) 3 欄、外資自營商 3 欄、外資合計 3 欄、投信 3 欄、
    # 自營商自行買賣 3 欄、自營商避險 3 欄、自營商合計 3 欄、三大法人合計
    rows = []
    for r in t["data"]:
        code = r[0].strip()
        if not STOCK_RE.match(code):
            continue
        rows.append({"symbol": code, "date": d.isoformat(), "foreign_net": num(r[10]),
                     "trust_net": num(r[13]), "dealer_net": num(r[22]), "total_net": num(r[23])})
    return rows


# ---------- 融資融券 ----------

def fetch_twse_margin(d: date):
    data = get_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d:%Y%m%d}&selectType=ALL&response=json")
    if data.get("stat") != "OK" or data.get("date") != f"{d:%Y%m%d}":
        return None
    t = _table(data, "融資融券彙總")
    if not t:
        return None
    return [{"symbol": r[0].strip(), "date": d.isoformat(), "margin_balance": num(r[6]), "short_balance": num(r[12])}
            for r in t["data"] if STOCK_RE.match(r[0].strip())]


def fetch_tpex_margin(d: date):
    data = get_json(f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={d:%Y/%m/%d}&response=json")
    t = _tpex_table(data, d)
    if not t:
        return None
    return [{"symbol": r[0].strip(), "date": d.isoformat(), "margin_balance": num(r[6]), "short_balance": num(r[14])}
            for r in t["data"] if STOCK_RE.match(r[0].strip())]


# ---------- 收集與回補 ----------

def _stamp(rows: list[dict]) -> list[dict]:
    now = iso()
    for r in rows:
        r["available_at"] = now
    return rows


def collect_quotes(conn, d: date) -> int:
    total = 0
    for fetch in (fetch_twse_quotes, fetch_tpex_quotes):
        res = fetch(d)
        if not res:
            continue
        quotes, indices = res
        instruments = [{"symbol": q["symbol"], "name": q["name"], "market": q["market"]} for q in quotes]
        conn.executemany(
            "INSERT INTO instruments (symbol, name, market, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, market=excluded.market, updated_at=excluded.updated_at",
            [(i["symbol"], i["name"], i["market"], iso()) for i in instruments])
        for q in quotes:
            q.pop("name")
        total += upsert(conn, "prices", _stamp(quotes))
        upsert(conn, "index_prices", indices)
    return total


def collect_flows(conn, d: date, parts: tuple[str, ...] = ("inst", "margin")) -> dict:
    out = {}
    for name, fetchers, table in (("inst", (fetch_twse_inst, fetch_tpex_inst), "institutional"),
                                  ("margin", (fetch_twse_margin, fetch_tpex_margin), "margin")):
        if name not in parts:
            continue
        n = 0
        for fetch in fetchers:
            rows = fetch(d)
            if rows:
                n += upsert(conn, table, _stamp(rows))
        out[name] = n
    return out


def has_quotes(conn, d: date, min_rows: int = 1500) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM prices WHERE date=? AND market IN ('TWSE','TPEX')", (d.isoformat(),)).fetchone()
    return row["n"] >= min_rows


def has_table_rows(conn, table: str, d: date, min_rows: int = 1000) -> bool:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE date=?", (d.isoformat(),)).fetchone()
    return row["n"] >= min_rows


def backfill(conn, price_days: int, flow_days: int, progress=print) -> None:
    """往回補齊最近 price_days 個交易日的行情，以及最近 flow_days 個交易日的籌碼。"""
    holidays = {r["date"] for r in conn.execute("SELECT date FROM holidays")}
    d = today_tw()
    if datetime.now(TW).hour < 15:
        d -= timedelta(days=1)
    got = 0
    while got < price_days:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            if not has_quotes(conn, d):
                n = collect_quotes(conn, d)
                if n == 0:
                    d -= timedelta(days=1)
                    continue
            got += 1
            if got <= flow_days and not (has_table_rows(conn, "institutional", d) and has_table_rows(conn, "margin", d)):
                collect_flows(conn, d)
            if got % 10 == 0:
                progress(f"回補進度：{got}/{price_days} 個交易日（目前 {d}）")
        d -= timedelta(days=1)


# ---------- 目前狀態類資料（只有最新一天） ----------

def _split_period(s: str) -> tuple[str, str]:
    parts = re.split(r"[～~]", s)
    return roc_to_date(parts[0]).isoformat(), roc_to_date(parts[-1]).isoformat()


def collect_flags(conn) -> int:
    rows = []
    for r in get_json(f"{TWSE_OPENAPI}/announcement/notice"):
        if STOCK_RE.match(r.get("Code", "").strip()) and r.get("Date"):
            d = roc_to_date(r["Date"]).isoformat()
            rows.append({"symbol": r["Code"].strip(), "kind": "notice", "start_date": d, "end_date": d,
                         "detail": r.get("TradingInfoForAttention", "")[:500]})
    for r in get_json(f"{TWSE_OPENAPI}/announcement/punish"):
        if STOCK_RE.match(r.get("Code", "").strip()) and r.get("DispositionPeriod"):
            start, end = _split_period(r["DispositionPeriod"])
            rows.append({"symbol": r["Code"].strip(), "kind": "punish", "start_date": start, "end_date": end,
                         "detail": r.get("ReasonsOfDisposition", "")})
    for r in get_json(f"{TPEX_OPENAPI}/tpex_trading_warning_information"):
        code = r.get("SecuritiesCompanyCode", "").strip()
        if STOCK_RE.match(code) and r.get("Date"):
            d = roc_to_date(r["Date"]).isoformat()
            rows.append({"symbol": code, "kind": "notice", "start_date": d, "end_date": d,
                         "detail": r.get("TradingInformation", "")[:500]})
    for r in get_json(f"{TPEX_OPENAPI}/tpex_disposal_information"):
        code = r.get("SecuritiesCompanyCode", "").strip()
        if STOCK_RE.match(code) and r.get("DispositionPeriod"):
            start, end = _split_period(r["DispositionPeriod"])
            rows.append({"symbol": code, "kind": "punish", "start_date": start, "end_date": end,
                         "detail": r.get("DispositionReasons", "")})
    return upsert(conn, "flags", _stamp(rows))


def collect_exdiv(conn) -> int:
    rows = []
    for r in get_json(f"{TWSE_OPENAPI}/exchangeReport/TWT48U_ALL"):
        if STOCK_RE.match(r.get("Code", "").strip()):
            rows.append({"symbol": r["Code"].strip(), "date": roc_to_date(r["Date"]).isoformat(),
                         "kind": r.get("Exdividend", ""), "cash_dividend": num(r.get("CashDividend"))})
    for r in get_json(f"{TPEX_OPENAPI}/tpex_exright_prepost"):
        code = r.get("SecuritiesCompanyCode", "").strip()
        if STOCK_RE.match(code):
            rows.append({"symbol": code, "date": roc_to_date(r["ExRrightsExDividendDate"]).isoformat(),
                         "kind": r.get("ExRrightsExDividend", ""), "cash_dividend": num(r.get("CashDividend"))})
    return upsert(conn, "exdiv", _stamp(rows))


def collect_revenue(conn) -> int:
    """月營收；同時更新 instruments 的產業別。保留第一次看到的時間作為 available_at。"""
    now, n = iso(), 0
    for url in (f"{TWSE_OPENAPI}/opendata/t187ap05_L", f"{TPEX_OPENAPI}/mopsfin_t187ap05_O"):
        for r in get_json(url):
            code = r.get("公司代號", "").strip()
            if not STOCK_RE.match(code):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO revenue (symbol, ym, revenue, yoy, mom, cum_yoy, available_at) VALUES (?,?,?,?,?,?,?)",
                (code, roc_ym(r["資料年月"]), num(r.get("營業收入-當月營收")), num(r.get("營業收入-去年同月增減(%)")),
                 num(r.get("營業收入-上月比較增減(%)")), num(r.get("累計營業收入-前期比較增減(%)")), now))
            n += cur.rowcount
            conn.execute("UPDATE instruments SET industry=? WHERE symbol=?", (r.get("產業別", "").strip(), code))
    conn.commit()
    return n


def collect_material_news(conn) -> int:
    """重大訊息 → events（官方公開資料）。"""
    now, n = iso(), 0
    sources = ((f"{TWSE_OPENAPI}/opendata/t187ap04_L", "公司代號", "主旨 ", "twse_mops"),
               (f"{TPEX_OPENAPI}/mopsfin_t187ap04_O", "SecuritiesCompanyCode", "主旨", "tpex_mops"))
    for url, code_key, title_key, source in sources:
        for r in get_json(url):
            code = r.get(code_key, "").strip()
            title = (r.get(title_key) or r.get(title_key.strip()) or "").replace("\r\n", " ").strip()
            if not STOCK_RE.match(code) or not title:
                continue
            t = str(r.get("發言時間", "0")).zfill(6)
            d = roc_to_date(r["發言日期"])
            published = datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:4]), int(t[4:6]), tzinfo=TW)
            uid = "mops:" + hashlib.sha1(f"{code}{r['發言日期']}{t}{title}".encode()).hexdigest()[:16]
            cur = conn.execute(
                "INSERT OR IGNORE INTO events (uid, kind, source, license_class, title, body, url, symbols, published_at, available_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, "material", source, "public", title, (r.get("說明") or "")[:3000], None,
                 dumps([code]), iso(published), now))
            n += cur.rowcount
    conn.commit()
    return n


def collect_holidays(conn) -> int:
    data = get_json("https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json")
    rows = [{"date": r[0], "name": r[1]} for r in data.get("data", [])
            if "開始交易" not in r[1] and "最後交易" not in r[1]]
    return upsert(conn, "holidays", rows)


def collect_futures_oi(conn) -> int:
    rows = get_json("https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
    out = [{"date": f"{r['Date'][:4]}-{r['Date'][4:6]}-{r['Date'][6:]}", "item": "foreign_tx",
            "oi_net": num(r.get("OpenInterest(Net)"))}
           for r in rows if r.get("ContractCode") == "臺股期貨" and r.get("Item") == "外資及陸資"]
    return upsert(conn, "futures_oi", out)


def is_trading_day(conn, d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return conn.execute("SELECT 1 FROM holidays WHERE date=?", (d.isoformat(),)).fetchone() is None
