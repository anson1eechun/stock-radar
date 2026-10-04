"""盤中監控：價格到點警報、論點失效監控。"""
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from . import llm
from .db import df, kv_get, kv_set
from .http import get_json
from .timeutil import iso, today_tw

log = logging.getLogger(__name__)


def realtime_quotes(conn, symbols: list[str]) -> dict[str, dict]:
    markets = {r["symbol"]: r["market"] for r in conn.execute(
        f"SELECT symbol, market FROM instruments WHERE symbol IN ({','.join('?' * len(symbols))})", symbols)}
    out = {}
    for i in range(0, len(symbols), 40):
        batch = symbols[i:i + 40]
        ex_ch = "|".join(f"{'otc' if markets.get(s) == 'TPEX' else 'tse'}_{s}.tw" for s in batch)
        data = get_json(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0")
        for m in data.get("msgArray", []):
            price = m.get("z")
            if price in (None, "-", ""):
                price = (m.get("b") or "").split("_")[0]
            try:
                p = float(price)
            except ValueError:
                continue
            if p > 0:
                out[m["c"]] = {"price": p, "time": m.get("t"), "high": m.get("h"), "low": m.get("l")}
    return out


def _once(conn, key: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO alerts_sent VALUES (?,?)", (key, iso()))
    conn.commit()
    return cur.rowcount == 1


def price_alerts(conn) -> list[str]:
    rows = df(conn, "SELECT r.id, r.symbol, r.name, r.entry_low, r.entry_high, r.target1, r.target2, "
                    "t.state, t.stop_current, t.t1_hit FROM recommendations r JOIN tracking t ON t.rec_id=r.id "
                    "WHERE r.mode='live' AND t.state IN ('pending','open')").to_dict("records")
    if not rows:
        return []
    quotes = realtime_quotes(conn, sorted({r["symbol"] for r in rows}))
    today, alerts = today_tw().isoformat(), []
    for r in rows:
        q = quotes.get(r["symbol"])
        if not q:
            continue
        p, tag = q["price"], f"#{r['id']} {r['symbol']} {r['name']}"
        if r["state"] == "pending" and r["entry_low"] <= p <= r["entry_high"] and _once(conn, f"{r['id']}:entry:{today}"):
            alerts.append(f"🟢 {tag} 進入買進區間 {r['entry_low']:g}–{r['entry_high']:g}，現價 {p:g}")
        elif r["state"] == "open":
            if r["stop_current"] and p <= r["stop_current"] and _once(conn, f"{r['id']}:stop:{today}"):
                alerts.append(f"🔴 {tag} 觸及停損 {r['stop_current']:g}，現價 {p:g}")
            elif not r["t1_hit"] and p >= r["target1"] and _once(conn, f"{r['id']}:t1:{today}"):
                alerts.append(f"🎯 {tag} 達第一目標 {r['target1']:g}，現價 {p:g}（建議出場一半，停損移至成本）")
            elif r["t1_hit"] and p >= r["target2"] and _once(conn, f"{r['id']}:t2:{today}"):
                alerts.append(f"🏁 {tag} 達第二目標 {r['target2']:g}，現價 {p:g}")
    return alerts


class InvalidationCheck(BaseModel):
    contradicts: bool = Field(description="這則新聞是否與原本的做多論點矛盾或觸發失效條件")
    severity: Literal["low", "medium", "high"] = Field(description="high＝論點已失效應出場；medium＝需要留意")
    reason: str = Field(description="60 字以內說明")


SYSTEM = ("你是風險控管員。判斷一則新聞是否與某筆做多建議的論點矛盾，或觸發它的失效條件。"
          "只根據新聞內容判斷；無關或影響輕微的新聞 contradicts=false。")


def check_invalidation(conn) -> list[dict]:
    since = kv_get(conn, "invalidation_since", iso())
    now = iso()
    recs = df(conn, "SELECT r.id, r.symbol, r.name, r.thesis, r.invalidation, t.state, t.invalidated_at "
                    "FROM recommendations r JOIN tracking t ON t.rec_id=r.id "
                    "WHERE r.mode='live' AND t.state IN ('pending','open')").to_dict("records")
    events = df(conn, "SELECT id, title, summary, sentiment, importance, symbols, available_at FROM events "
                      "WHERE processed=1 AND available_at>? AND sentiment<=0.3 AND importance>=0.3", (since,)).to_dict("records")
    alerts = []
    for ev in events:
        syms = set(json.loads(ev["symbols"] or "[]"))
        for r in recs:
            if r["symbol"] not in syms:
                continue
            if conn.execute("SELECT 1 FROM invalidation_alerts WHERE rec_id=? AND event_id=?", (r["id"], ev["id"])).fetchone():
                continue
            user = (f"建議：做多 {r['symbol']} {r['name']}\n論點：{r['thesis']}\n失效條件：{'；'.join(json.loads(r['invalidation'] or '[]'))}\n\n"
                    f"新聞：{ev['title']}\n摘要：{ev['summary']}\n情緒：{ev['sentiment']:+.1f}，重要性 {ev['importance']:.1f}")
            obj, analysis_id = llm.ask(conn, "invalidation", SYSTEM, user, InvalidationCheck, "invalidation-v1",
                                       thinking=True, max_tokens=500)
            if obj is None:
                continue
            conn.execute("INSERT OR IGNORE INTO invalidation_alerts (rec_id, created_at, event_id, reason, severity, analysis_id) "
                         "VALUES (?,?,?,?,?,?)", (r["id"], iso(), ev["id"], obj.reason, obj.severity if obj.contradicts else "none",
                                                  analysis_id))
            if obj.contradicts and obj.severity == "high" and not r["invalidated_at"]:
                conn.execute("UPDATE tracking SET invalidated_at=? WHERE rec_id=?", (ev["available_at"], r["id"]))
                r["invalidated_at"] = ev["available_at"]
            conn.commit()
            if obj.contradicts and obj.severity in ("medium", "high"):
                alerts.append({"rec_id": r["id"], "symbol": r["symbol"], "name": r["name"], "severity": obj.severity,
                               "reason": obj.reason, "title": ev["title"]})
    kv_set(conn, "invalidation_since", now)
    return alerts


def format_invalidation(a: dict) -> str:
    head = "⛔ 論點失效，建議出場" if a["severity"] == "high" else "⚠️ 論點受挑戰，請留意"
    return f"{head}｜#{a['rec_id']} {a['symbol']} {a['name']}\n新聞：{a['title']}\n判斷：{a['reason']}"
