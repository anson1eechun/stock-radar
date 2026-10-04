"""建議庫：組合篩選、價位、風控，寫入不可修改的建議與追蹤列。"""
import logging

import pandas as pd

from . import levels, risk, screening
from .config import cfg
from .db import dumps, insert_ignore
from .timeutil import iso

log = logging.getLogger(__name__)


def rule_thesis(screen: str, f: dict) -> tuple[str, list[str]]:
    c, atr = f["close"], f["atr"]
    if screen == "breakout":
        return (f"收盤 {c:g} 突破前 55 日高點 {f['hh55_prev']:g}，量能為 20 日均量 {f['vol_ratio']:.1f} 倍，均線多頭排列",
                [f"收盤跌回 {levels.round_tick(f['hh55_prev'] - 0.5 * atr):g} 以下（突破失敗）"])
    if screen == "inst_accumulation":
        return (f"外資加投信近 5 日有 {int(f['inst_buy_days5'])} 日買超，合計佔成交量 {f['inst_ratio5']:.0%}，股價站上月線",
                ["外資與投信連續 3 日轉為賣超", f"收盤跌破月線 {levels.round_tick(f['ma20']):g}"])
    if screen == "pullback":
        return (f"月線 {f['ma20']:.1f} 在季線 {f['ma60']:.1f} 之上的多頭趨勢中，拉回至月線附近，RSI {f['rsi14']:.0f}",
                [f"收盤跌破季線 {levels.round_tick(f['ma60']):g}"])
    if screen == "revenue_momentum":
        return (f"{f['rev_ym']} 營收年增 {f['rev_yoy']:.0f}%、累計年增 {f['rev_cum_yoy']:.0f}%，股價在季線之上",
                ["下個月營收年增率轉負", f"收盤跌破季線 {levels.round_tick(f['ma60']):g}"])
    if screen == "window_dressing":
        return (f"季底作帳期間，投信近 10 日買超佔成交量 {f['trust_ratio10']:.0%}",
                ["投信連續 3 日賣超", "季底後作帳效應結束"])
    return "", []


def build_pick(feat: pd.DataFrame, symbol: str, screen: str, score: float, strategy: str | None = None,
               horizon: str | None = None) -> dict:
    f = screening.feature_dict(feat, symbol)
    default_strategy, default_horizon = screening.STRATEGY[screen]
    strategy, horizon = strategy or default_strategy, horizon or default_horizon
    lv = levels.compute(strategy, horizon, f["close"], f["ma20"], f["atr"])
    row = feat.loc[symbol]
    return {"symbol": symbol, "name": row.get("name"), "industry": row.get("industry"), "screen": screen,
            "strategy": strategy, "horizon": horizon, "score": round(float(score), 3), "features": f,
            "priced_in": levels.priced_in(f["close"], f["ma20"], f["atr"]), **lv}


def issue(conn, grp: str, mode: str, asof: str, issued_at: str, picks: list[dict]) -> list[int]:
    ids = []
    for p in picks:
        if p.get("excluded"):
            continue
        rec_id = insert_ignore(conn, "recommendations", {
            "grp": grp, "mode": mode, "issued_at": issued_at, "asof_date": asof, "symbol": p["symbol"],
            "name": p.get("name"), "direction": "long", "horizon": p["horizon"], "strategy": p["strategy"],
            "entry_low": p["entry_low"], "entry_high": p["entry_high"], "stop": p["stop"],
            "target1": p["target1"], "target2": p["target2"], "time_stop_days": p["time_stop_days"],
            "entry_window_days": p["entry_window_days"], "confidence": p.get("confidence"),
            "thesis": p.get("thesis"), "invalidation": dumps(p.get("invalidation", [])),
            "sources": dumps(p.get("sources", [])), "risk_notes": dumps(p.get("risk_notes", [])),
            "priced_in": p.get("priced_in"), "size_pct": p.get("size_pct"), "score": p.get("score"),
            "analysis_id": p.get("analysis_id")})
        if rec_id:
            conn.execute("INSERT INTO tracking (rec_id, state, last_date, updated_at) VALUES (?,?,?,?)",
                         (rec_id, "pending", asof, iso()))
            conn.commit()
            ids.append(rec_id)
    return ids


def deprecated_screens(conn) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM signal_stats WHERE kind='screen' AND status='deprecated'")}


def replay(conn, start: str, end: str | None = None, progress=print) -> int:
    """用歷史資料逐日重跑純規則組（mode=replay）。只用每天收盤時已知的資料；AI 組無法重播（模型知道未來）。"""
    from . import tracking
    end = end or screening.latest_trading_date(conn)
    dates = [d for d in screening.trading_dates(conn, end, 400) if d >= start]
    total = 0
    for i, asof in enumerate(dates):
        tracking.update(conn, asof)
        known_at = screening.known_at_for(asof)
        feat = screening.compute_features(conn, asof, known_at)
        screens = screening.run_screens(feat, asof)
        ids, _ = run_rule_group(conn, asof, feat, screens, known_at, mode="replay", issued_at=known_at)
        total += len(ids)
        if (i + 1) % 10 == 0:
            progress(f"重播進度 {i + 1}/{len(dates)}（{asof}），累計 {total} 筆建議")
    tracking.update(conn, end)
    return total


def run_rule_group(conn, asof: str, feat: pd.DataFrame, screens: pd.DataFrame, known_at: str,
                   mode: str = "live", issued_at: str | None = None) -> tuple[list[int], list[dict]]:
    k = cfg()["screening"]["rule_top_k"]
    ranked = screening.rank(screens, screening.RULE_SCREENS, deprecated_screens(conn))
    picks = []
    for sym, r in ranked.head(k * 3).iterrows():
        p = build_pick(feat, sym, r["screen"], r["score"])
        thesis, invalidation = rule_thesis(r["screen"], p["features"])
        p.update(thesis=thesis, invalidation=invalidation, sources=[f"screen:{s}" for s in r["screens"]])
        picks.append(p)
    assessed = risk.assess(conn, picks, "rule", mode, asof, known_at)
    accepted = [p for p in assessed if not p["excluded"]][:k]
    ids = issue(conn, "rule", mode, asof, issued_at or iso(), accepted)
    return ids, assessed
