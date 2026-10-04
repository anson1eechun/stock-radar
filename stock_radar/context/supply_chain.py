"""美國企業到台灣供應鏈的傳導、隔夜美股映射、美國企業財報摘要。"""
import logging
from datetime import date, timedelta
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .. import llm
from ..collectors.news import google_news
from ..config import ROOT
from ..db import kv_get, kv_set
from ..timeutil import us_cutoff_date

log = logging.getLogger(__name__)
MOVE_THRESHOLD = 0.02
LABELS = {"^SOX": "費半", "^GSPC": "標普500", "^IXIC": "那斯達克", "^VIX": "VIX", "^TNX": "美債10年",
          "DX-Y.NYB": "美元指數", "TWD=X": "美元兌台幣", "TSM": "台積電ADR"}


@lru_cache(maxsize=1)
def graph() -> dict:
    with open(ROOT / "supply_chain.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _last_two(conn, symbol: str, cutoff: str):
    rows = conn.execute("SELECT date, close FROM prices WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 2",
                        (symbol, cutoff)).fetchall()
    return rows if len(rows) == 2 else None


def us_moves(conn, known_at: str) -> dict[str, dict]:
    cutoff = us_cutoff_date(known_at)
    out = {}
    for sym in list(graph()) + ["^GSPC", "^IXIC", "TSM", "^VIX", "^TNX", "DX-Y.NYB", "TWD=X"]:
        rows = _last_two(conn, sym, cutoff)
        if rows:
            out[sym] = {"date": rows[0]["date"], "close": rows[0]["close"], "ret": rows[0]["close"] / rows[1]["close"] - 1}
    return out


def adr_premium(conn, known_at: str) -> float | None:
    cutoff = us_cutoff_date(known_at)
    tsm = conn.execute("SELECT close FROM prices WHERE symbol='TSM' AND date<=? ORDER BY date DESC LIMIT 1", (cutoff,)).fetchone()
    fx = conn.execute("SELECT close FROM prices WHERE symbol='TWD=X' AND date<=? ORDER BY date DESC LIMIT 1", (known_at[:10],)).fetchone()
    tw = conn.execute("SELECT close FROM prices WHERE symbol='2330' AND date<=? ORDER BY date DESC LIMIT 1", (known_at[:10],)).fetchone()
    if not (tsm and fx and tw):
        return None
    return tsm["close"] * fx["close"] / 5 / tw["close"] - 1


def mapped_impacts(conn, known_at: str) -> list[dict]:
    """把隔夜美股漲跌依權重傳導到台灣個股。只計入漲跌超過門檻的美股。"""
    moves = us_moves(conn, known_at)
    names = {r["symbol"]: r["name"] for r in conn.execute("SELECT symbol, name FROM instruments")}
    impact: dict[str, dict] = {}
    for us, spec in graph().items():
        m = moves.get(us)
        if not m or abs(m["ret"]) < MOVE_THRESHOLD:
            continue
        for link in spec["links"]:
            d = impact.setdefault(link["symbol"], {"symbol": link["symbol"], "name": names.get(link["symbol"], ""),
                                                   "score": 0.0, "reasons": []})
            d["score"] += link["weight"] * m["ret"]
            d["reasons"].append(f"{spec['name']} {m['ret']:+.1%}（{link['relation']}）")
    return sorted(impact.values(), key=lambda d: -abs(d["score"]))


def overnight_text(conn, known_at: str) -> str:
    moves = us_moves(conn, known_at)
    if not moves:
        return "隔夜美股：無資料"
    us_date = max(m["date"] for m in moves.values())
    parts = []
    for sym in ("^SOX", "^IXIC", "^GSPC", "NVDA", "AAPL", "TSM"):
        if sym in moves:
            parts.append(f"{LABELS.get(sym, sym)} {moves[sym]['ret']:+.1%}")
    text = f"隔夜美股（{us_date}）：" + "、".join(parts)
    extra = []
    prem = adr_premium(conn, known_at)
    if prem is not None:
        extra.append(f"台積電 ADR 溢價 {prem:+.1%}")
    for sym, fmt in (("^VIX", "{:.1f}"), ("^TNX", "{:.2f}%"), ("TWD=X", "{:.2f}")):
        if sym in moves:
            extra.append(f"{LABELS[sym]} " + fmt.format(moves[sym]["close"]) + f"（{moves[sym]['ret']:+.1%}）")
    if extra:
        text += "\n" + "；".join(extra)
    impacts = [i for i in mapped_impacts(conn, known_at) if abs(i["score"]) >= 0.01][:8]
    if impacts:
        text += "\n供應鏈傳導：" + "；".join(f"{i['symbol']} {i['name']} {'偏多' if i['score'] > 0 else '偏空'}"
                                         f"（{'、'.join(i['reasons'][:2])}）" for i in impacts)
    return text


def links_text(conn, symbol: str, known_at: str) -> str:
    moves = us_moves(conn, known_at)
    parts = []
    for us, spec in graph().items():
        for link in spec["links"]:
            if link["symbol"] == symbol:
                m = moves.get(us)
                move = f"隔夜 {m['ret']:+.1%}" if m else "無資料"
                parts.append(f"{spec['name']}（{link['relation']}，權重 {link['weight']}）{move}")
    return ("美國供應鏈連動：" + "；".join(parts)) if parts else ""


# ---------- 美國企業財報 ----------

def earnings_dates(conn) -> dict[str, list[str]]:
    """各美國企業的財報日期（每天快取一次）。"""
    cache = kv_get(conn, "earnings_dates")
    today = date.today().isoformat()
    if cache and cache.get("fetched") == today:
        return cache["dates"]
    import yfinance as yf
    dates = {}
    for sym in graph():
        if sym.startswith("^"):
            continue
        try:
            ed = yf.Ticker(sym).get_earnings_dates(limit=6)
            dates[sym] = sorted({ts.tz_convert("America/New_York").date().isoformat() for ts in ed.index})
        except Exception as e:
            log.info("取得 %s 財報日期失敗：%s", sym, e)
    kv_set(conn, "earnings_dates", {"fetched": today, "dates": dates})
    return dates


class EarningsDigest(BaseModel):
    summary: str = Field(description="財報與財測重點，80 字以內中文")
    sentiment: float = Field(ge=-1, le=1, description="對台灣供應鏈的影響")
    tw_implication: str = Field(description="對台灣供應鏈的可能影響，80 字以內，只能根據標題推論")
    confidence: Literal["low", "medium", "high"]


def recent_earnings(conn, known_at: str, days: int = 2) -> list[dict]:
    """最近 days 天內公布財報的供應鏈美國企業，附上新聞摘要（每家每次財報只分析一次）。"""
    cutoff = date.fromisoformat(us_cutoff_date(known_at))
    out = []
    for sym, dates in earnings_dates(conn).items():
        # 只看已經公布的財報：美東當天盤後公布的，要等當天收盤後才算已知
        hit = [d for d in dates if cutoff - timedelta(days=days) <= date.fromisoformat(d) <= cutoff]
        if not hit:
            continue
        key = f"earnings_digest:{sym}:{hit[-1]}"
        digest = kv_get(conn, key)
        if digest is None:
            spec = graph()[sym]
            headlines = google_news(f"{sym} earnings guidance", "en", 10)
            if not headlines:
                continue
            user = (f"公司：{sym} {spec['name']}，財報日 {hit[-1]}\n相關台灣供應鏈："
                    + "、".join(f"{l['symbol']}（{l['relation']}）" for l in spec["links"])
                    + "\n最新新聞標題：\n" + "\n".join(f"- {h['published'][:16]} {h['title']}" for h in headlines))
            obj, _ = llm.ask(conn, "earnings", "你是美股財報分析師，根據新聞標題摘要財報重點與對台灣供應鏈的影響。不可編造標題中沒有的數字。",
                             user, EarningsDigest, "earnings-v1", thinking=False, max_tokens=600)
            if obj is None:
                continue
            digest = obj.model_dump()
            kv_set(conn, key, digest)
        out.append({"symbol": sym, "name": graph()[sym]["name"], "date": hit[-1], **digest})
    return out
