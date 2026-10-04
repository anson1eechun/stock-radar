"""量化初篩：計算特徵、跑篩選規則、產生候選池。所有計算都只用 asof（含）以前的資料。"""
import numpy as np
import pandas as pd

from .config import cfg
from .db import df, dumps
from .timeutil import quarter_end

# 篩選規則 → (策略模板, 持有週期)
STRATEGY = {
    "breakout": ("breakout", "swing"),
    "inst_accumulation": ("trend", "swing"),
    "pullback": ("pullback", "swing"),
    "revenue_momentum": ("trend", "position"),
    "window_dressing": ("trend", "swing"),
    "news_catalyst": ("event", "swing"),
}
# 純規則組只能用不經過 AI 的篩選
RULE_SCREENS = ("breakout", "inst_accumulation", "pullback", "revenue_momentum", "window_dressing")

SCREEN_LABEL = {
    "breakout": "突破", "inst_accumulation": "法人連買", "pullback": "趨勢回檔",
    "revenue_momentum": "營收成長", "window_dressing": "投信作帳", "news_catalyst": "新聞催化",
}


def known_at_for(asof: str) -> str:
    return f"{asof}T23:59:59+08:00"


def trading_dates(conn, end: str, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT date FROM index_prices WHERE name='發行量加權股價指數' AND date<=? ORDER BY date DESC LIMIT ?",
        (end, n)).fetchall()
    return [r["date"] for r in reversed(rows)]


def latest_trading_date(conn, end: str | None = None) -> str | None:
    dates = trading_dates(conn, end or "9999-12-31", 1)
    return dates[0] if dates else None


def _pivot(frame: pd.DataFrame, col: str) -> pd.DataFrame:
    return frame.pivot(index="date", columns="symbol", values=col).sort_index()


def compute_features(conn, asof: str, known_at: str | None = None) -> pd.DataFrame:
    known_at = known_at or known_at_for(asof)
    dates = trading_dates(conn, asof, 130)
    if len(dates) < 20:
        raise RuntimeError(f"{asof} 之前的行情資料不足（只有 {len(dates)} 天）")
    px = df(conn, "SELECT symbol, date, open, high, low, close, volume, value FROM prices "
                  "WHERE market IN ('TWSE','TPEX') AND date>=? AND date<=?", (dates[0], asof))
    o, h, l, c = (_pivot(px, k) for k in ("open", "high", "low", "close"))
    vol, val = _pivot(px, "volume"), _pivot(px, "value")
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()]).groupby(level=0).max().sort_index()
    period = cfg()["levels"]["atr_period"]
    atr = tr.rolling(period, min_periods=period).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    last = c.index[-1]
    feat = pd.DataFrame({
        "close": c.loc[last],
        "ma20": c.rolling(20).mean().loc[last],
        "ma60": c.rolling(60).mean().loc[last],
        "atr": atr.loc[last],
        "hh55_prev": h.rolling(55, min_periods=40).max().shift(1).loc[last],
        "ll10": l.rolling(10).min().loc[last],
        "vol_ratio": (vol.loc[last] / vol.rolling(20).mean().shift(1).loc[last]),
        "avg_value20": val.rolling(20).mean().loc[last],
        "rsi14": rsi.loc[last],
        "ret5": c.loc[last] / c.shift(5).loc[last] - 1,
        "ret20": c.loc[last] / c.shift(20).loc[last] - 1,
        "n_days": c.notna().sum(),
        "traded_today": c.loc[last].notna(),
    })
    feat["atr_pct"] = feat["atr"] / feat["close"]
    feat["dist_ma20_atr"] = (feat["close"] - feat["ma20"]) / feat["atr"]

    # 三大法人
    flow_dates = dates[-10:]
    inst = df(conn, "SELECT symbol, date, foreign_net, trust_net FROM institutional WHERE date>=? AND date<=?",
              (flow_dates[0], asof))
    if not inst.empty:
        fn, tn = _pivot(inst, "foreign_net").reindex(flow_dates), _pivot(inst, "trust_net").reindex(flow_dates)
        both = fn.fillna(0) + tn.fillna(0)
        v10 = vol.reindex(flow_dates)
        feat["inst_buy_days5"] = (both.tail(5) > 0).sum()
        feat["inst_ratio5"] = both.tail(5).sum() / v10.tail(5).sum()
        feat["trust_ratio10"] = tn.fillna(0).sum() / v10.sum()
        feat["foreign_net5"] = fn.tail(5).sum()
        feat["trust_net5"] = tn.tail(5).sum()

    # 月營收：只用決策當下已經知道、且是近兩個月的資料
    rev = df(conn, "SELECT symbol, ym, yoy, mom, cum_yoy FROM revenue WHERE available_at<=? ORDER BY ym", (known_at,))
    if not rev.empty:
        latest = rev.groupby("symbol").tail(1).set_index("symbol")
        min_ym = (pd.Timestamp(asof) - pd.DateOffset(months=2)).strftime("%Y-%m")
        latest = latest[latest["ym"] >= min_ym]
        feat = feat.join(latest.rename(columns={"ym": "rev_ym", "yoy": "rev_yoy", "mom": "rev_mom",
                                                 "cum_yoy": "rev_cum_yoy"}))

    info = df(conn, "SELECT symbol, name, market, industry FROM instruments").set_index("symbol")
    feat = feat.join(info)
    feat.index.name = "symbol"
    return feat


def universe(feat: pd.DataFrame) -> pd.DataFrame:
    u = cfg()["universe"]
    mask = (feat["traded_today"] & (feat["n_days"] >= u["min_history_days"]) & (feat["close"] >= u["min_price"])
            & (feat["avg_value20"] >= u["min_avg_value"]) & feat["atr"].notna() & feat["ma60"].notna())
    return feat[mask]


def _clip(x, lo=0.0, hi=1.0):
    return np.clip(x, lo, hi)


def run_screens(feat: pd.DataFrame, asof: str) -> pd.DataFrame:
    f = universe(feat)
    get = lambda col: f[col] if col in f else pd.Series(np.nan, index=f.index)
    results = []

    # 分數相近時，離月線越遠（越可能已反映）排越後面
    extension_penalty = 0.03 * (f["dist_ma20_atr"] - 1.5).clip(lower=0)

    def add(name, mask, score):
        mask = mask.fillna(False)
        for sym in f.index[mask]:
            results.append({"symbol": sym, "screen": name, "score": round(float(score[sym] - extension_penalty[sym]), 4)})

    extended = f["dist_ma20_atr"] > 3
    add("breakout",
        (f["close"] > f["hh55_prev"]) & (f["close"] > f["ma20"]) & (f["ma20"] > f["ma60"]) & (f["vol_ratio"] >= 1.5),
        (0.5 * _clip((f["vol_ratio"] - 1.5) / 2.5) + 0.5 * _clip((f["close"] / f["ma60"] - 1) / 0.2))
        * np.where(extended, 0.5, 1.0))
    add("inst_accumulation",
        (get("inst_buy_days5") >= 4) & (get("inst_ratio5") >= 0.08) & (f["close"] > f["ma20"]),
        0.7 * _clip(get("inst_ratio5") / 0.25) + 0.3 * (get("inst_buy_days5") == 5))
    add("pullback",
        (f["ma20"] > f["ma60"]) & (f["close"] > f["ma60"]) & f["dist_ma20_atr"].between(-0.6, 0.6)
        & f["rsi14"].between(38, 55) & (f["ret20"] > 0),
        0.5 + 0.5 * _clip((f["ma20"] / f["ma60"] - 1) / 0.1))
    add("revenue_momentum",
        (get("rev_yoy") >= 30) & (get("rev_cum_yoy") >= 15) & (get("rev_mom") >= -10) & (f["close"] > f["ma60"]),
        0.6 * _clip(get("rev_yoy") / 100) + 0.4 * _clip(get("rev_cum_yoy") / 60))
    d = pd.Timestamp(asof).date()
    if (quarter_end(d) - d).days <= cfg()["screening"]["window_dressing_days"]:
        add("window_dressing",
            (get("trust_ratio10") >= 0.03) & (f["close"] > f["ma20"]),
            _clip(get("trust_ratio10") / 0.1))
    return pd.DataFrame(results, columns=["symbol", "screen", "score"])


FEATURE_COLS = ["close", "ma20", "ma60", "atr", "atr_pct", "hh55_prev", "vol_ratio", "rsi14", "ret5", "ret20",
                "dist_ma20_atr", "inst_buy_days5", "inst_ratio5", "trust_ratio10", "foreign_net5", "trust_net5",
                "rev_ym", "rev_yoy", "rev_mom", "rev_cum_yoy", "avg_value20"]


def feature_dict(feat: pd.DataFrame, symbol: str) -> dict:
    row = feat.loc[symbol]
    out = {}
    for k in FEATURE_COLS:
        v = row.get(k)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        out[k] = round(float(v), 4) if isinstance(v, (int, float, np.floating, np.integer)) else v
    return out


def save_candidates(conn, asof: str, screens: pd.DataFrame, feat: pd.DataFrame) -> None:
    conn.executemany("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?)",
                     [(asof, r.symbol, r.screen, r.score, dumps(feature_dict(feat, r.symbol)))
                      for r in screens.itertuples()])
    conn.commit()


def rank(screens: pd.DataFrame, allowed: tuple[str, ...], deprecated: set[str] = frozenset()) -> pd.DataFrame:
    """每檔股票取最高分的篩選；同時命中多個篩選加分。"""
    s = screens[screens["screen"].isin(allowed) & ~screens["screen"].isin(deprecated)]
    if s.empty:
        return s.assign(n_screens=[], screens=[])
    g = s.groupby("symbol")
    best = s.loc[g["score"].idxmax()].set_index("symbol")
    best["n_screens"] = g.size()
    best["screens"] = g["screen"].apply(list)
    best["score"] = best["score"] + 0.1 * (best["n_screens"] - 1)
    return best.sort_values("score", ascending=False)
