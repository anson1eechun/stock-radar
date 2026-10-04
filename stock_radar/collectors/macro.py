"""美國總經行事曆（ForexFactory 預期值）與實際值（BLS、FRED、Fed）。"""
import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser

from ..config import cfg, secret
from ..http import get_json, get_text, request
from ..timeutil import TW, iso, now_tw

log = logging.getLogger(__name__)

FF_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
           "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FED_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"


def parse_value(s: str | None) -> float | None:
    """'201K' → 201、'0.3%' → 0.3、'1.2M' → 1200（以千為單位）。"""
    if not s:
        return None
    m = re.match(r"^\s*([<>]?)(-?\d+(?:\.\d+)?)\s*([KMB%]?)", s)
    if not m:
        return None
    v = float(m.group(2))
    return v * 1000 if m.group(3) == "M" else v


def event_spec(title: str) -> dict | None:
    return cfg()["macro"]["events"].get(title)


def surprise_class(actual: float | None, forecast: float | None, tol: float) -> str | None:
    if actual is None or forecast is None:
        return None
    diff = actual - forecast
    if diff > tol + 1e-9:
        return "above"
    if diff < -tol - 1e-9:
        return "below"
    return "inline"


# ---------- 實際值來源 ----------

def bls_series(series_ids: list[str]) -> dict[str, list[tuple[str, float]]]:
    """回傳 {序列: [(YYYY-MM, 值), ...]}，依時間由舊到新。"""
    year = now_tw().year
    body = {"seriesid": series_ids, "startyear": str(year - 1), "endyear": str(year)}
    if secret("BLS_API_KEY"):
        body["registrationkey"] = secret("BLS_API_KEY")
    data = request("POST", BLS_URL, json=body).json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API：{data.get('message')}")
    out = {}
    for s in data["Results"]["series"]:
        obs = [(f"{x['year']}-{x['period'][1:]}", float(x["value"])) for x in s["data"]
               if x["period"].startswith("M") and x["period"] != "M13" and x["value"] not in ("-", "")]
        out[s["seriesID"]] = sorted(obs)
    return out


def fred_series(series_id: str) -> list[tuple[str, float]]:
    text = get_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    rows = list(csv.reader(io.StringIO(text)))[1:]
    return [(r[0], float(r[1])) for r in rows if len(r) == 2 and r[1] not in (".", "")]


def fed_statement(after: datetime) -> tuple[str, float] | None:
    """找出 after 之後發布的 FOMC 聲明，回傳（日期, 聯邦基金利率目標區間上限）。"""
    feed = feedparser.parse(get_text(FED_RSS))
    for e in feed.entries:
        if "FOMC statement" not in e.get("title", ""):
            continue
        published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).astimezone(TW)
        if published < after:
            continue
        upper = parse_fed_upper(get_text(e.link))
        if upper is not None:
            return published.date().isoformat(), upper
    return None


def _frac(s: str) -> float:
    s = s.strip()
    if "-" in s:
        whole, frac = s.split("-", 1)
        n, d = frac.split("/")
        return float(whole) + float(n) / float(d)
    if "/" in s:
        n, d = s.split("/")
        return float(n) / float(d)
    return float(s)


def parse_fed_upper(html: str) -> float | None:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"target range for the federal funds rate (?:by [\d\-/]+ percentage points? )?(?:at|to) "
                  r"([\d\-/]+) to ([\d\-/]+) percent", text)
    return _frac(m.group(2)) if m else None


def compute(transform: str, obs: list[tuple[str, float]]) -> tuple[str, float] | None:
    if not obs:
        return None
    period, last = obs[-1]
    prev = obs[-2][1] if len(obs) > 1 else None
    if transform == "level":
        return period, last
    if transform == "thousands":
        return period, last / 1000
    if prev is None:
        return None
    if transform == "diff":
        return period, last - prev
    if transform == "pct_mom":
        return period, round((last / prev - 1) * 100, 1)
    raise ValueError(transform)


def latest_period(spec: dict) -> str | None:
    if spec["source"] == "bls":
        obs = bls_series([spec["series"]]).get(spec["series"], [])
    elif spec["source"] == "fred":
        obs = fred_series(spec["series"])
    else:
        return None
    return obs[-1][0] if obs else None


# ---------- 行事曆 ----------

def refresh_calendar(conn) -> int:
    n = 0
    for url in FF_URLS:
        try:
            events = get_json(url)
        except Exception as e:  # 下週行事曆通常週末才公布，之前會回 404
            log.info("ForexFactory %s 無法取得：%s", url, e)
            continue
        for e in events:
            spec = event_spec(e["title"])
            if e["country"] != "USD" or (e["impact"] not in ("High", "Medium") and not spec):
                continue
            scheduled = iso(datetime.fromisoformat(e["date"]))
            conn.execute(
                "INSERT INTO macro_calendar (title, country, scheduled_at, impact, event_key, forecast, previous, "
                "forecast_value, available_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(title, country, scheduled_at) DO UPDATE SET impact=excluded.impact, "
                "forecast=excluded.forecast, previous=excluded.previous, forecast_value=excluded.forecast_value, "
                "updated_at=excluded.updated_at",
                (e["title"], e["country"], scheduled, e["impact"], spec["key"] if spec else None,
                 e.get("forecast"), e.get("previous"), parse_value(e.get("forecast")), iso(), iso()))
            n += 1
    conn.commit()
    _fill_pre_periods(conn)
    return n


def _fill_pre_periods(conn) -> None:
    """公布前先記下目前最新一期，之後出現更新的一期就代表已公布。"""
    rows = conn.execute(
        "SELECT id, title FROM macro_calendar WHERE event_key IS NOT NULL AND pre_period IS NULL "
        "AND actual_value IS NULL AND scheduled_at > ?", (iso(),)).fetchall()
    for r in rows:
        spec = event_spec(r["title"])
        if not spec or spec["source"] not in ("bls", "fred"):
            continue
        try:
            period = latest_period(spec)
        except Exception as e:
            log.warning("取得 %s 最新一期失敗：%s", r["title"], e)
            continue
        conn.execute("UPDATE macro_calendar SET pre_period=? WHERE id=?", (period, r["id"]))
    conn.commit()


def key_events_between(conn, start: datetime, end: datetime, impact_high_only: bool = False) -> list[dict]:
    sql = ("SELECT * FROM macro_calendar WHERE event_key IS NOT NULL AND scheduled_at >= ? AND scheduled_at < ?"
           + (" AND impact='High'" if impact_high_only else "") + " ORDER BY scheduled_at")
    return [dict(r) for r in conn.execute(sql, (iso(start), iso(end)))]


def check_actuals(conn, rows: list[dict]) -> list[dict]:
    """檢查一組同時公布的事件是否已有實際值；回傳這次新取得實際值的事件。"""
    updated = []
    pending = [r for r in rows if r["actual_value"] is None]
    bls_ids = [event_spec(r["title"])["series"] for r in pending if event_spec(r["title"])["source"] == "bls"]
    bls_data = bls_series(bls_ids) if bls_ids else {}
    for r in pending:
        spec = event_spec(r["title"])
        result = None
        if spec["source"] == "bls":
            result = compute(spec["transform"], bls_data.get(spec["series"], []))
        elif spec["source"] == "fred":
            result = compute(spec["transform"], fred_series(spec["series"]))
        elif spec["source"] == "fed":
            scheduled = datetime.fromisoformat(r["scheduled_at"])
            result = fed_statement(scheduled - timedelta(minutes=5))
        if not result:
            continue
        period, value = result
        if spec["source"] in ("bls", "fred") and r["pre_period"] and period <= r["pre_period"]:
            continue
        cls = surprise_class(value, r["forecast_value"], spec["tol"])
        surprise = value - r["forecast_value"] if r["forecast_value"] is not None else None
        conn.execute(
            "UPDATE macro_calendar SET actual_value=?, actual_period=?, surprise=?, surprise_class=?, "
            "actual_source=?, actual_at=? WHERE id=?",
            (value, period, surprise, cls, spec["source"], iso(), r["id"]))
        conn.commit()
        updated.append({**r, "actual_value": value, "actual_period": period, "surprise": surprise,
                        "surprise_class": cls, "actual_source": spec["source"]})
    return updated
