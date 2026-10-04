"""報告文字產生。"""
import json

from .. import levels
from ..db import df

GRP_LABEL = {"ai": "AI 組", "rule": "純規則組"}
HORIZON_LABEL = {"swing": "波段", "position": "中長線"}
STRATEGY_LABEL = {"breakout": "突破", "pullback": "回檔", "trend": "趨勢", "event": "事件"}
REASON_LABEL = {"stop": "停損", "breakeven": "保本出場", "target2": "第二目標", "time": "時間停損",
                "invalidated": "論點失效", "not_filled": "未成交", "gap_below_stop": "跳空低於停損"}
REGIME_LABEL = {"risk_on": "風險偏好", "neutral": "中性", "risk_off": "風險趨避"}


def _j(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (TypeError, ValueError):
        return default if default is not None else []


def rec_card(r: dict) -> str:
    mid = (r["entry_low"] + r["entry_high"]) / 2
    stop_pct = (r["stop"] / mid - 1) * 100
    c = r.get("confidence")
    conf = f"｜信心 {c:.2f}" if c is not None and c == c else ""  # c == c 排除 NaN
    lines = [
        f"**#{r['id']} {r['symbol']} {r.get('name') or ''}**｜{GRP_LABEL.get(r['grp'], r['grp'])}｜"
        f"{HORIZON_LABEL.get(r['horizon'], r['horizon'])}・{STRATEGY_LABEL.get(r['strategy'], r['strategy'])}{conf}",
        f"買進區間 {r['entry_low']:g}–{r['entry_high']:g}｜停損 {r['stop']:g}（{stop_pct:.1f}%）｜"
        f"目標 {r['target1']:g} / {r['target2']:g}",
        f"建議部位 {r['size_pct']:g}%｜{r['entry_window_days']} 日內未成交失效｜時間停損 {r['time_stop_days']} 日｜"
        f"{levels.priced_in_label(r.get('priced_in'))}",
    ]
    if r.get("thesis"):
        lines.append(f"理由：{r['thesis']}")
    inval = _j(r.get("invalidation"))
    if inval:
        lines.append("失效條件：" + "；".join(s.rstrip("。；") for s in inval))
    notes = _j(r.get("risk_notes"))
    if notes:
        lines.append("風控：" + "；".join(notes))
    return "\n".join(lines)


def recs_for(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return df(conn, f"SELECT * FROM recommendations WHERE id IN ({q}) ORDER BY grp, score DESC", ids).to_dict("records")


def active_positions(conn, mode: str = "live") -> list[dict]:
    return df(conn, "SELECT r.id, r.grp, r.symbol, r.name, r.entry_low, r.entry_high, r.stop, r.target1, r.target2, "
                    "t.state, t.fill_price, t.fill_date, t.stop_current, t.t1_hit, "
                    "(SELECT close FROM prices p WHERE p.symbol=r.symbol ORDER BY date DESC LIMIT 1) AS last_close "
                    "FROM recommendations r JOIN tracking t ON t.rec_id=r.id "
                    "WHERE r.mode=? AND t.state IN ('pending','open') ORDER BY t.state, r.grp, r.id", (mode,)).to_dict("records")


def positions_section(conn) -> str:
    rows = active_positions(conn)
    if not rows:
        return "【進行中建議】無"
    out = ["【進行中建議】"]
    for r in rows:
        if r["state"] == "open":
            r0 = r["fill_price"] - r["stop"]
            ur = (r["last_close"] - r["fill_price"]) / r0 if r0 and r["last_close"] else 0
            t1 = "，已達第一目標" if r["t1_hit"] else ""
            out.append(f"・#{r['id']} {r['symbol']} {r['name']}（{GRP_LABEL[r['grp']]}）持有中：成本 {r['fill_price']:g}，"
                       f"收盤 {r['last_close']:g}，{ur:+.2f}R，停損 {r['stop_current']:g}{t1}")
        else:
            out.append(f"・#{r['id']} {r['symbol']} {r['name']}（{GRP_LABEL[r['grp']]}）等待進場：{r['entry_low']:g}–{r['entry_high']:g}")
    return "\n".join(out)


def changes_section(changes: dict) -> str:
    out = []
    for info in changes.get("filled", []):
        out.append(f"・成交 #{info['rec_id']} {info['symbol']} {info['name']} @ {info['fill_price']:g}")
    for info in changes.get("t1", []):
        out.append(f"・#{info['rec_id']} {info['symbol']} 達第一目標，出場一半，停損移至成本")
    for info in changes.get("closed", []):
        out.append(f"・出場 #{info['rec_id']} {info['symbol']} {info['name']}：{REASON_LABEL.get(info['exit_reason'], info['exit_reason'])}"
                   f" @ {info['exit_price']:g}，{info['r_multiple']:+.2f}R")
    for key in ("expired", "cancelled"):
        for info in changes.get(key, []):
            out.append(f"・#{info['rec_id']} {info['symbol']} {REASON_LABEL.get(info['exit_reason'], info['exit_reason'])}，取消")
    return "【今日變化】\n" + ("\n".join(out) if out else "無")


def performance_section(conn, mode: str = "live") -> str:
    d = df(conn, "SELECT r.grp, t.r_multiple, t.bench_return FROM recommendations r JOIN tracking t ON t.rec_id=r.id "
                 "WHERE r.mode=? AND t.state='closed'", (mode,))
    if d.empty:
        return "【累積績效】尚無結案建議"
    out = ["【累積績效】"]
    for grp, g in d.groupby("grp"):
        out.append(f"・{GRP_LABEL.get(grp, grp)}：{len(g)} 筆，勝率 {(g['r_multiple'] > 0).mean():.0%}，"
                   f"平均 {g['r_multiple'].mean():+.2f}R，合計 {g['r_multiple'].sum():+.1f}R")
    return "\n".join(out)


def macro_section(conn, start_iso: str, end_iso: str, title: str) -> str:
    rows = conn.execute("SELECT title, scheduled_at, impact, forecast, previous, actual_value, surprise_class "
                        "FROM macro_calendar WHERE scheduled_at>=? AND scheduled_at<? AND (impact='High' OR event_key IS NOT NULL) "
                        "ORDER BY scheduled_at", (start_iso, end_iso)).fetchall()
    if not rows:
        return f"【{title}】無重要數據"
    out = [f"【{title}】"]
    cls = {"above": "高於預期", "below": "低於預期", "inline": "符合預期"}
    for r in rows:
        t = r["scheduled_at"][11:16]
        actual = f"，實際 {r['actual_value']:g}（{cls.get(r['surprise_class'], '—')}）" if r["actual_value"] is not None else ""
        out.append(f"・{r['scheduled_at'][5:10]} {t} {r['title']}｜預期 {r['forecast'] or '—'}｜前值 {r['previous'] or '—'}{actual}")
    return "\n".join(out)
