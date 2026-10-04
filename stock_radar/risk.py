"""風控：部位大小、集中度、市場狀態、事件前降曝險、處置/注意股、除權息。"""
from datetime import date, datetime, timedelta

from .config import cfg


def next_trading_dates(conn, after: str, n: int) -> list[str]:
    holidays = {r["date"] for r in conn.execute("SELECT date FROM holidays")}
    d, out = date.fromisoformat(after), []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d.isoformat())
    return out


def regime_state(conn, asof: str) -> str:
    row = conn.execute("SELECT state FROM market_regime WHERE date<=? ORDER BY date DESC LIMIT 1", (asof,)).fetchone()
    return row["state"] if row else "neutral"


def upcoming_high_impact(conn, known_at: str, until_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT title, scheduled_at, event_key FROM macro_calendar WHERE impact='High' AND event_key IS NOT NULL "
        "AND scheduled_at>=? AND scheduled_at<=? ORDER BY scheduled_at", (known_at, f"{until_date}T23:59:59+08:00"))
    return [dict(r) for r in rows]


def open_positions(conn, grp: str, mode: str) -> list[dict]:
    rows = conn.execute(
        "SELECT r.symbol, i.industry FROM recommendations r JOIN tracking t ON t.rec_id=r.id "
        "LEFT JOIN instruments i ON i.symbol=r.symbol WHERE r.grp=? AND r.mode=? AND t.state IN ('pending','open')",
        (grp, mode))
    return [dict(r) for r in rows]


def assess(conn, picks: list[dict], grp: str, mode: str, asof: str, known_at: str) -> list[dict]:
    """為每個候選加上 size_pct 與 risk_notes；不符合條件的標記 excluded。picks 需依優先順序排列。"""
    rc = cfg()["risk"]
    first_day = next_trading_dates(conn, asof, 1)[0]
    blackout_until = next_trading_dates(conn, asof, rc["event_blackout_days"])[-1]
    state = regime_state(conn, asof)
    events = upcoming_high_impact(conn, known_at, blackout_until)
    held = open_positions(conn, grp, mode)
    held_symbols = {h["symbol"] for h in held}
    industry_count: dict[str, int] = {}
    for h in held:
        industry_count[h["industry"]] = industry_count.get(h["industry"], 0) + 1
    total = len(held)

    out = []
    for p in picks:
        notes, excluded = [], None
        sym = p["symbol"]
        punish = conn.execute("SELECT end_date FROM flags WHERE symbol=? AND kind='punish' AND start_date<=? AND end_date>=? "
                              "AND available_at<=?", (sym, first_day, first_day, known_at)).fetchone()
        if punish:
            excluded = f"處置股（至 {punish['end_date']}）"
        elif sym in held_symbols:
            excluded = "已有有效建議"
        elif total >= rc["max_open"]:
            excluded = f"有效建議已達上限 {rc['max_open']} 檔"
        elif industry_count.get(p.get("industry"), 0) >= rc["max_per_industry"]:
            excluded = f"{p.get('industry')} 已有 {rc['max_per_industry']} 檔"

        notice = conn.execute("SELECT start_date FROM flags WHERE symbol=? AND kind='notice' AND start_date>=? AND available_at<=?",
                              (sym, (date.fromisoformat(asof) - timedelta(days=5)).isoformat(), known_at)).fetchone()
        if notice:
            notes.append(f"近期列為注意股（{notice['start_date']}）")
        horizon_end = next_trading_dates(conn, asof, p["time_stop_days"] + p["entry_window_days"])[-1]
        ex = conn.execute("SELECT date, kind, cash_dividend FROM exdiv WHERE symbol=? AND date>=? AND date<=?",
                          (sym, first_day, horizon_end)).fetchone()
        if ex:
            notes.append(f"{ex['date']} 除{ex['kind'].replace('除', '')}，價位需依除權息調整")

        mid = (p["entry_low"] + p["entry_high"]) / 2
        stop_pct = (mid - p["stop"]) / mid * 100
        size = min(rc["risk_per_trade_pct"] / stop_pct * 100, rc["max_position_pct"])
        mult = rc["regime_size_multiplier"].get(state, 1.0)
        if mult < 1:
            notes.append(f"市場狀態 {state}，部位 ×{mult}")
        if events:
            mult *= 0.5
            names = "、".join(sorted({e["event_key"] for e in events}))
            notes.append(f"{names} 公布前，部位減半")
        size = round(size * mult, 1)

        if not excluded:
            total += 1
            industry_count[p.get("industry")] = industry_count.get(p.get("industry"), 0) + 1
            held_symbols.add(sym)
        out.append({**p, "size_pct": size, "risk_notes": notes, "excluded": excluded, "stop_pct": round(stop_pct, 1)})
    return out
