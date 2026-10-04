"""績效歸因與淘汰、交易行為分析。"""
import json
import math

import pandas as pd

from .config import cfg
from .db import df
from .timeutil import iso


def _metrics(r: pd.Series) -> dict:
    n = len(r)
    sd = r.std(ddof=1) if n > 1 else float("nan")
    t = r.mean() / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    return {"n": n, "win_rate": float((r > 0).mean()), "avg_r": float(r.mean()), "t_stat": float(t)}


def _status(m: dict) -> str:
    tc = cfg()["tracking"]
    if m["n"] >= tc["deprecate_min_n"] and m["t_stat"] < tc["deprecate_t_stat"]:
        return "deprecated"
    if m["n"] >= tc["probation_min_n"] and m["avg_r"] < 0:
        return "probation"
    return "active"


def closed_trades(conn, modes=("live",)) -> pd.DataFrame:
    q = ",".join("?" * len(modes))
    return df(conn, f"SELECT r.id, r.grp, r.mode, r.symbol, r.strategy, r.sources, r.stop, t.fill_price, t.exit_price, "
                    f"t.exit_date, t.exit_reason, t.r_multiple, t.bench_return FROM recommendations r "
                    f"JOIN tracking t ON t.rec_id=r.id WHERE t.state='closed' AND r.mode IN ({q})", modes)


def update_signal_stats(conn) -> pd.DataFrame:
    """group 與 strategy 只用 live；screen 也納入純規則組的歷史重播（重播只用當時已知資料，可以參考）。"""
    rows = []
    live = closed_trades(conn, ("live",))
    both = closed_trades(conn, ("live", "replay"))
    for grp, g in live.groupby("grp"):
        rows.append(("group", grp, _metrics(g["r_multiple"])))
    for strat, g in live.groupby("strategy"):
        rows.append(("strategy", strat, _metrics(g["r_multiple"])))
    exploded = both.assign(source=both["sources"].map(lambda s: json.loads(s or "[]"))).explode("source").dropna(subset=["source"])
    for src, g in exploded.groupby("source"):
        kind, key = src.split(":", 1)
        rows.append((kind, key, _metrics(g["r_multiple"])))
    now = iso()
    conn.executemany("INSERT OR REPLACE INTO signal_stats VALUES (?,?,?,?,?,?,?,?)",
                     [(k, key, m["n"], m["win_rate"], m["avg_r"], m["t_stat"], _status(m), now) for k, key, m in rows])
    conn.commit()
    return df(conn, "SELECT * FROM signal_stats ORDER BY kind, n DESC")


def stats_text(conn) -> str:
    s = df(conn, "SELECT * FROM signal_stats ORDER BY kind, n DESC")
    if s.empty:
        return "【訊號績效】尚無結案紀錄"
    label = {"group": "組別", "screen": "篩選規則", "news": "新聞類別", "strategy": "策略模板"}
    status = {"active": "", "probation": "（觀察中）", "deprecated": "（已淘汰）"}
    out = ["【訊號績效】"]
    for kind, g in s.groupby("kind", sort=False):
        out.append(f"{label.get(kind, kind)}：" + "；".join(
            f"{r['key']} {r['n']} 筆 勝率 {r['win_rate']:.0%} 平均 {r['avg_r']:+.2f}R{status.get(r['status'], '')}"
            for _, r in g.iterrows()))
    return "\n".join(out)


def behavior_text(conn) -> str:
    """比較使用者實際進場的建議與全部建議；分析實際出場與系統出場的差異。"""
    acts = df(conn, "SELECT a.rec_id, a.action, a.price, a.at FROM user_actions a ORDER BY a.at")
    if acts.empty:
        return "【交易行為】尚無進場回報（Discord 按鈕、儀表板或 `stock-radar took <編號>`）"
    took = acts[acts["action"] == "took"].groupby("rec_id").last()
    exits = acts[acts["action"] == "exit"].groupby("rec_id").last()
    skipped = set(acts[acts["action"] == "skip"]["rec_id"])
    all_closed = closed_trades(conn).set_index("id")
    out = ["【交易行為】"]
    taken_closed = all_closed[all_closed.index.isin(took.index)]
    others = all_closed[~all_closed.index.isin(took.index)]
    out.append(f"・回報進場 {len(took)} 筆、略過 {len(skipped)} 筆")
    if len(taken_closed) and len(others):
        out.append(f"・你挑選的建議系統績效 {taken_closed['r_multiple'].mean():+.2f}R（{len(taken_closed)} 筆），"
                   f"未挑選的 {others['r_multiple'].mean():+.2f}R（{len(others)} 筆）")
    recs = df(conn, "SELECT r.id, r.entry_high, r.stop, r.target1, t.r_multiple, t.exit_price, t.state "
                    "FROM recommendations r JOIN tracking t ON t.rec_id=r.id").set_index("id")
    slippage, early, held_past_stop, actual_r = [], 0, 0, []
    for rec_id, a in took.iterrows():
        if rec_id not in recs.index or pd.isna(a["price"]):
            continue
        r = recs.loc[rec_id]
        slippage.append(a["price"] / r["entry_high"] - 1)
        if rec_id in exits.index and not pd.isna(exits.loc[rec_id, "price"]):
            px = exits.loc[rec_id, "price"]
            risk = a["price"] - r["stop"]
            if risk > 0:
                actual_r.append((px - a["price"]) / risk)
            if px < r["stop"] * 0.99:
                held_past_stop += 1
            if r["state"] == "closed" and r["r_multiple"] is not None and risk > 0 and (px - a["price"]) / risk < r["r_multiple"] - 0.5 and px > a["price"]:
                early += 1
    if slippage:
        out.append(f"・進場價平均比買進區間上緣 {sum(slippage) / len(slippage):+.1%}（正值代表追高）")
    if actual_r:
        out.append(f"・實際出場 {len(actual_r)} 筆，平均 {sum(actual_r) / len(actual_r):+.2f}R")
    if early:
        out.append(f"・{early} 筆比系統規則更早停利，少賺超過 0.5R")
    if held_past_stop:
        out.append(f"・{held_past_stop} 筆出場價低於停損 1% 以上（停損沒有確實執行）")
    return "\n".join(out)
