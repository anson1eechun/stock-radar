"""總經事件歷史反應資料庫。

兩種資料來源：
1. live：系統實際記錄的「市場預期 vs 實際值」，每次公布後自動累積（basis=vs_forecast）。
2. 歷史回補：FRED/ALFRED 的首次公布值與公布日（需 FRED_API_KEY）。沒有歷史市場預期值，
   所以改用「這次數值 vs 上次數值」的變化分三等分（basis=vs_prev）。
   FOMC 升降息改用 FRED 利率上限的變動（不需金鑰，basis=decision）。
"""
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..collectors import us_market
from ..collectors.macro import fred_series
from ..config import secret
from ..db import df, upsert
from ..http import get_json

log = logging.getLogger(__name__)
TARGETS = {"^TWII": "加權指數", "2330.TW": "台積電", "TWD=X": "美元兌台幣"}
FRED_HISTORY = {"NFP": ("PAYEMS", "diff"), "UNRATE": ("UNRATE", "level"), "CPI": ("CPIAUCSL", "pct_mom"),
                "CORE_CPI": ("CPILFESL", "pct_mom"), "PPI": ("PPIFIS", "pct_mom"),
                "CORE_PCE": ("PCEPILFE", "pct_mom"), "RETAIL": ("RSAFS", "pct_mom")}
BUCKET_LABEL = {"above": "高於預期", "inline": "符合預期", "below": "低於預期", "hotter": "比上次高",
                "flat": "與上次相近", "cooler": "比上次低", "hike": "升息", "cut": "降息"}


def _reaction(prices: pd.Series, release_date: str) -> tuple[float | None, float | None]:
    """公布日（台灣時間當晚或隔日凌晨）後第一個交易日的收盤，相對公布日當天（或之前最近）收盤的報酬。"""
    before = prices[prices.index <= release_date]
    after = prices[prices.index > release_date]
    if before.empty or after.empty:
        return None, None
    base = before.iloc[-1]
    r1 = after.iloc[0] / base - 1
    r5 = after.iloc[4] / base - 1 if len(after) >= 5 else None
    return float(r1), (float(r5) if r5 is not None else None)


def _price_map(conn) -> dict[str, pd.Series]:
    return {t: us_market.closes(conn, t, limit=10000) for t in TARGETS}


def ensure_history(conn, start: str = "2012-01-01") -> None:
    row = conn.execute("SELECT MIN(date) FROM prices WHERE symbol='^TWII'").fetchone()[0]
    if row is None or row > start:
        us_market.collect(conn, list(TARGETS), start=start)


def backfill_fomc(conn) -> int:
    """FRED 利率上限變動 → 升降息日期（生效日前一天為決議日）。"""
    ensure_history(conn)
    obs = fred_series("DFEDTARU")
    prices, rows = _price_map(conn), []
    for (d0, v0), (d1, v1) in zip(obs, obs[1:]):
        if v1 == v0:
            continue
        decision = (date.fromisoformat(d1) - timedelta(days=1)).isoformat()
        for t, s in prices.items():
            r1, r5 = _reaction(s, decision)
            rows.append({"event_key": "FOMC", "release_date": decision, "value": v1, "surprise": v1 - v0,
                         "bucket": "hike" if v1 > v0 else "cut", "basis": "decision", "target": t, "ret_1d": r1, "ret_5d": r5})
    return upsert(conn, "event_reactions", rows)


def _fred_initial(series_id: str, start: str) -> list[tuple[str, str, float]]:
    """回傳 [(資料期間, 首次公布日, 首次公布值)]。"""
    data = get_json("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": series_id, "api_key": secret("FRED_API_KEY"), "file_type": "json",
        "output_type": 4, "observation_start": start})
    return [(o["date"], o["realtime_start"], float(o["value"])) for o in data["observations"] if o["value"] not in (".", "")]


def backfill_fred(conn, start: str = "2012-01-01") -> int:
    if not secret("FRED_API_KEY"):
        raise RuntimeError("需要 FRED_API_KEY 才能回補歷史首次公布值（免費申請：https://fred.stlouisfed.org/docs/api/api_key.html）")
    ensure_history(conn, start)
    prices, total = _price_map(conn), 0
    for key, (sid, transform) in FRED_HISTORY.items():
        obs = _fred_initial(sid, start)
        headline = []
        for (p0, _, v0), (p1, released, v1) in zip(obs, obs[1:]):
            value = v1 - v0 if transform == "diff" else v1 if transform == "level" else round((v1 / v0 - 1) * 100, 1)
            headline.append((released, value))
        if len(headline) < 10:
            continue
        changes = np.diff([v for _, v in headline])
        lo, hi = np.quantile(changes, [1 / 3, 2 / 3])
        rows = []
        for (released, value), chg in zip(headline[1:], changes):
            bucket = "hotter" if chg > hi else "cooler" if chg < lo else "flat"
            for t, s in prices.items():
                r1, r5 = _reaction(s, released)
                rows.append({"event_key": key, "release_date": released, "value": value, "surprise": float(chg),
                             "bucket": bucket, "basis": "vs_prev", "target": t, "ret_1d": r1, "ret_5d": r5})
        total += upsert(conn, "event_reactions", rows)
    return total


def update_live(conn) -> int:
    """把已公布實際值、且隔天台股已收盤的事件寫入反應資料庫。"""
    ensure_history(conn, "2024-01-01")
    rows_in = conn.execute("SELECT event_key, scheduled_at, actual_value, surprise, surprise_class FROM macro_calendar "
                           "WHERE actual_value IS NOT NULL AND surprise_class IS NOT NULL").fetchall()
    prices, rows = _price_map(conn), []
    for r in rows_in:
        # 台股 13:30 收盤；凌晨公布的事件（例如 FOMC）要以前一天收盤為基準
        release = r["scheduled_at"][:10]
        if r["scheduled_at"][11:16] < "13:30":
            release = (date.fromisoformat(release) - timedelta(days=1)).isoformat()
        for t, s in prices.items():
            r1, r5 = _reaction(s, release)
            if r1 is None:
                continue
            rows.append({"event_key": r["event_key"], "release_date": release, "value": r["actual_value"],
                         "surprise": r["surprise"], "bucket": r["surprise_class"], "basis": "vs_forecast",
                         "target": t, "ret_1d": r1, "ret_5d": r5})
    return upsert(conn, "event_reactions", rows)


def summary(conn, event_key: str) -> pd.DataFrame:
    d = df(conn, "SELECT * FROM event_reactions WHERE event_key=? AND ret_1d IS NOT NULL", (event_key,))
    if d.empty:
        return d
    return (d.groupby(["basis", "bucket", "target"])
             .agg(n=("ret_1d", "size"), mean_1d=("ret_1d", "mean"), up_rate=("ret_1d", lambda x: (x > 0).mean()),
                  mean_5d=("ret_5d", "mean"))
             .reset_index())


def summary_text(conn, event_key: str) -> str:
    s = summary(conn, event_key)
    if s.empty:
        return f"{event_key} 歷史反應：尚無資料"
    lines = [f"{event_key} 歷史反應（公布後第一個台股交易日）："]
    for (basis, bucket), g in s.groupby(["basis", "bucket"]):
        parts = []
        for _, r in g.iterrows():
            five = f"，5日 {r['mean_5d']:+.2%}" if pd.notna(r["mean_5d"]) else ""
            parts.append(f"{TARGETS.get(r['target'], r['target'])} 平均 {r['mean_1d']:+.2%}（上漲機率 {r['up_rate']:.0%}{five}）")
        lines.append(f"・{BUCKET_LABEL.get(bucket, bucket)}（{int(g['n'].iloc[0])} 次，{basis}）：" + "；".join(parts))
    return "\n".join(lines)
