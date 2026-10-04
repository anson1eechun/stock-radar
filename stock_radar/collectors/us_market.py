"""美股、匯率、利率、台股指數（yfinance）。"""
import logging
from datetime import timedelta

import pandas as pd
import yfinance as yf

from ..config import cfg
from ..db import upsert
from ..timeutil import iso, today_tw

log = logging.getLogger(__name__)


def download(tickers: list[str], start: str) -> dict[str, pd.DataFrame]:
    data = yf.download(tickers, start=start, auto_adjust=False, group_by="ticker",
                       progress=False, threads=True)
    out = {}
    for t in tickers:
        if t not in data.columns.get_level_values(0):
            continue
        sub = data[t].dropna(subset=["Close"])
        if not sub.empty:
            out[t] = sub
    return out


def collect(conn, tickers: list[str] | None = None, start: str | None = None) -> int:
    tickers = tickers or cfg()["us_market"]["tickers"]
    start = start or (today_tw() - timedelta(days=cfg()["us_market"]["history_days"])).isoformat()
    now, n = iso(), 0
    for t, sub in download(tickers, start).items():
        rows = [{"symbol": t, "date": idx.date().isoformat(), "open": float(r["Open"]), "high": float(r["High"]),
                 "low": float(r["Low"]), "close": float(r["Close"]),
                 "volume": float(r["Volume"]) if pd.notna(r["Volume"]) else None,
                 "value": None, "market": "YF", "available_at": now}
                for idx, r in sub.iterrows()]
        n += upsert(conn, "prices", rows)
    return n


def closes(conn, symbol: str, end: str | None = None, limit: int = 400) -> pd.Series:
    sql = "SELECT date, close FROM prices WHERE symbol=?" + (" AND date<=?" if end else "") + " ORDER BY date DESC LIMIT ?"
    params = (symbol, end, limit) if end else (symbol, limit)
    rows = conn.execute(sql, params).fetchall()
    return pd.Series({r["date"]: r["close"] for r in reversed(rows)}, dtype=float)
