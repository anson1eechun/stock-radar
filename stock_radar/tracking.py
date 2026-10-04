"""虛擬成交追蹤：用每日 K 棒模擬成交、停損、停利、時間停損、論點失效出場。

保守假設：同一根 K 棒同時碰到停損與目標時算停損；成交當天不檢查目標。
第一目標出場一半並把停損移到成本；第二目標、停損或時間到則全部出場。
"""
from datetime import datetime

from .db import df
from .timeutil import iso

TAIEX = "發行量加權股價指數"


def _bench(conn, start: str, end: str) -> float | None:
    rows = conn.execute("SELECT date, close FROM index_prices WHERE name=? AND date IN (?,?)", (TAIEX, start, end)).fetchall()
    m = {r["date"]: r["close"] for r in rows}
    return m[end] / m[start] - 1 if start in m and end in m else None


def _invalidation_exit(invalidated_at: str | None, d: str, o: float, c: float) -> float | None:
    if not invalidated_at:
        return None
    inv = datetime.fromisoformat(invalidated_at)
    inv_date = inv.date().isoformat()
    if inv_date > d:
        return None
    if inv_date < d or inv.hour < 9:
        return o
    return c


def step(rec: dict, t: dict, bar: dict, days_since_issue: int, days_held: int) -> dict:
    """處理一根 K 棒，回傳更新後的追蹤狀態。"""
    t = dict(t)
    d, o, h, l, c = bar["date"], bar["open"], bar["high"], bar["low"], bar["close"]
    if o is None or c is None:
        return t
    h, l = h or max(o, c), l or min(o, c)

    if t["state"] == "pending":
        if _invalidation_exit(t.get("invalidated_at"), d, o, c) is not None:
            return {**t, "state": "cancelled", "exit_reason": "invalidated", "exit_date": d}
        if days_since_issue > rec["entry_window_days"]:
            return {**t, "state": "expired", "exit_reason": "not_filled", "exit_date": d}
        if l > rec["entry_high"]:
            return t
        if o <= rec["stop"]:
            return {**t, "state": "cancelled", "exit_reason": "gap_below_stop", "exit_date": d}
        fill = min(o, rec["entry_high"])
        t.update(state="open", fill_date=d, fill_price=fill, stop_current=rec["stop"], t1_hit=0, realized_r=0.0,
                 mfe_r=0.0, mae_r=0.0)
        r0 = fill - rec["stop"]
        t["mae_r"] = min(0.0, (l - fill) / r0)
        if l <= rec["stop"]:
            return _close(t, rec, d, rec["stop"], "stop")
        return t

    if t["state"] != "open":
        return t
    fill, r0 = t["fill_price"], t["fill_price"] - rec["stop"]
    t["mfe_r"] = max(t.get("mfe_r") or 0.0, (h - fill) / r0)
    t["mae_r"] = min(t.get("mae_r") or 0.0, (l - fill) / r0)

    px = _invalidation_exit(t.get("invalidated_at"), d, o, c)
    if px is not None:
        return _close(t, rec, d, px, "invalidated")
    if l <= t["stop_current"]:
        return _close(t, rec, d, min(o, t["stop_current"]), "breakeven" if t["t1_hit"] else "stop")
    if not t["t1_hit"] and h >= rec["target1"]:
        t["realized_r"] = 0.5 * (max(o, rec["target1"]) - fill) / r0
        t["t1_hit"] = 1
        t["stop_current"] = fill
    if t["t1_hit"] and h >= rec["target2"]:
        return _close(t, rec, d, max(o, rec["target2"]), "target2")
    if days_held >= rec["time_stop_days"]:
        return _close(t, rec, d, c, "time")
    return t


def _close(t: dict, rec: dict, d: str, px: float, reason: str) -> dict:
    r0 = t["fill_price"] - rec["stop"]
    remaining = 0.5 if t["t1_hit"] else 1.0
    r = (t.get("realized_r") or 0.0) + remaining * (px - t["fill_price"]) / r0
    return {**t, "state": "closed", "exit_date": d, "exit_price": round(px, 2), "exit_reason": reason,
            "r_multiple": round(r, 3)}


def update(conn, until: str) -> dict:
    """把所有進行中的建議推進到 until（含）。回傳這次狀態有變化的清單。"""
    active = df(conn, "SELECT r.*, t.state, t.fill_date, t.fill_price, t.stop_current, t.t1_hit, t.realized_r, "
                      "t.mfe_r, t.mae_r, t.invalidated_at, t.last_date FROM recommendations r "
                      "JOIN tracking t ON t.rec_id=r.id WHERE t.state IN ('pending','open')")
    changes = {"filled": [], "closed": [], "expired": [], "cancelled": [], "t1": []}
    for rec in active.to_dict("records"):
        bars = conn.execute("SELECT date, open, high, low, close FROM prices WHERE symbol=? AND date>? AND date<=? "
                            "ORDER BY date", (rec["symbol"], rec["last_date"], until)).fetchall()
        if not bars:
            continue
        issued_days = conn.execute("SELECT COUNT(*) FROM index_prices WHERE name=? AND date>? AND date<?",
                                   (TAIEX, rec["asof_date"], bars[0]["date"])).fetchone()[0]
        held_days = 0
        if rec["fill_date"]:
            held_days = conn.execute("SELECT COUNT(*) FROM index_prices WHERE name=? AND date>? AND date<?",
                                     (TAIEX, rec["fill_date"], bars[0]["date"])).fetchone()[0]
        t = {k: rec[k] for k in ("state", "fill_date", "fill_price", "stop_current", "t1_hit", "realized_r",
                                 "mfe_r", "mae_r", "invalidated_at")}
        before = dict(t)
        for bar in bars:
            issued_days += 1
            if t["state"] == "open" and t["fill_date"] != bar["date"]:
                held_days += 1
            t = step(rec, t, dict(bar), issued_days, held_days)
            if t["state"] not in ("pending", "open"):
                break
        last = bars[-1]["date"] if t["state"] in ("pending", "open") else t.get("exit_date", bars[-1]["date"])
        if t["state"] == "closed":
            t["bench_return"] = _bench(conn, t["fill_date"], t["exit_date"])
        conn.execute(
            "UPDATE tracking SET state=?, fill_date=?, fill_price=?, stop_current=?, t1_hit=?, realized_r=?, "
            "exit_date=?, exit_price=?, exit_reason=?, r_multiple=?, bench_return=?, mfe_r=?, mae_r=?, "
            "last_date=?, updated_at=? WHERE rec_id=?",
            (t["state"], t.get("fill_date"), t.get("fill_price"), t.get("stop_current"), t.get("t1_hit") or 0,
             t.get("realized_r") or 0, t.get("exit_date"), t.get("exit_price"), t.get("exit_reason"),
             t.get("r_multiple"), t.get("bench_return"), t.get("mfe_r"), t.get("mae_r"), last, iso(), rec["id"]))
        info = {"rec_id": rec["id"], "symbol": rec["symbol"], "name": rec["name"], "grp": rec["grp"], **t}
        if before["state"] == "pending" and t["state"] in ("open", "closed"):
            changes["filled"].append(info)
        if t["state"] in ("closed", "expired", "cancelled"):
            changes[t["state"]].append(info)
        if not before.get("t1_hit") and t.get("t1_hit"):
            changes["t1"].append(info)
    conn.commit()
    return changes
