"""市場狀態儀表：綜合美債、美元、台幣、VIX、大盤趨勢、外資現貨與期貨籌碼。"""
import json

from ..db import dumps
from ..timeutil import iso, us_cutoff_date

STATE_LABEL = {"risk_on": "風險偏好", "neutral": "中性", "risk_off": "風險趨避"}


def _series(conn, symbol: str, cutoff: str, n: int = 70) -> list[float]:
    rows = conn.execute("SELECT close FROM prices WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
                        (symbol, cutoff, n)).fetchall()
    return [r["close"] for r in reversed(rows)]


def _score(value, pos, neg) -> int:
    """value 滿足 pos 條件 +1、滿足 neg 條件 -1。"""
    if value is None:
        return 0
    return 1 if pos(value) else -1 if neg(value) else 0


def compute(conn, asof: str, known_at: str | None = None) -> dict:
    known_at = known_at or f"{asof}T23:59:59+08:00"
    us_cut = us_cutoff_date(known_at)
    inputs, scores = {}, {}

    vix = _series(conn, "^VIX", us_cut, 1)
    inputs["vix"] = vix[-1] if vix else None
    scores["vix"] = _score(inputs["vix"], lambda v: v < 18, lambda v: v > 25)

    tnx = _series(conn, "^TNX", us_cut, 21)
    inputs["us10y_chg20"] = round(tnx[-1] - tnx[0], 3) if len(tnx) == 21 else None
    scores["us10y"] = _score(inputs["us10y_chg20"], lambda v: v < -0.15, lambda v: v > 0.25)

    dxy = _series(conn, "DX-Y.NYB", us_cut, 21)
    inputs["dxy_chg20"] = round(dxy[-1] / dxy[0] - 1, 4) if len(dxy) == 21 else None
    scores["dxy"] = _score(inputs["dxy_chg20"], lambda v: v < -0.01, lambda v: v > 0.015)

    twd = _series(conn, "TWD=X", asof, 21)
    inputs["usdtwd_chg20"] = round(twd[-1] / twd[0] - 1, 4) if len(twd) == 21 else None
    scores["twd"] = _score(inputs["usdtwd_chg20"], lambda v: v < -0.01, lambda v: v > 0.01)

    rows = conn.execute("SELECT close FROM index_prices WHERE name='發行量加權股價指數' AND date<=? ORDER BY date DESC LIMIT 60",
                        (asof,)).fetchall()
    closes = [r["close"] for r in reversed(rows)]
    if len(closes) == 60:
        ma20, ma60 = sum(closes[-20:]) / 20, sum(closes) / 60
        inputs["taiex"] = {"close": closes[-1], "ma20": round(ma20, 1), "ma60": round(ma60, 1)}
        scores["taiex"] = 1 if closes[-1] > ma20 > ma60 else -1 if closes[-1] < ma60 else 0
    else:
        scores["taiex"] = 0

    oi = conn.execute("SELECT oi_net FROM futures_oi WHERE item='foreign_tx' AND date<=? ORDER BY date DESC LIMIT 6",
                      (asof,)).fetchall()
    inputs["foreign_oi_chg5"] = oi[0]["oi_net"] - oi[-1]["oi_net"] if len(oi) == 6 else None
    inputs["foreign_oi"] = oi[0]["oi_net"] if oi else None
    scores["foreign_oi"] = _score(inputs["foreign_oi_chg5"], lambda v: v > 5000, lambda v: v < -5000)

    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM institutional WHERE date<=? ORDER BY date DESC LIMIT 5", (asof,))]
    if len(dates) == 5:
        q = ",".join("?" * 5)
        v = conn.execute(f"SELECT SUM(i.foreign_net * p.close) FROM institutional i JOIN prices p "
                         f"ON p.symbol=i.symbol AND p.date=i.date WHERE i.date IN ({q})", dates).fetchone()[0]
        inputs["foreign_spot5_bn"] = round((v or 0) / 1e8, 1)
        scores["foreign_spot"] = _score(inputs["foreign_spot5_bn"], lambda x: x > 300, lambda x: x < -300)
    else:
        scores["foreign_spot"] = 0

    total = sum(scores.values())
    state = "risk_on" if total >= 2 else "risk_off" if total <= -2 else "neutral"
    result = {"date": asof, "state": state, "score": total, "inputs": inputs, "scores": scores}
    conn.execute("INSERT OR REPLACE INTO market_regime VALUES (?,?,?,?,?)",
                 (asof, state, total, dumps({"inputs": inputs, "scores": scores}), iso()))
    conn.commit()
    return result


def describe(conn, asof: str) -> str:
    row = conn.execute("SELECT * FROM market_regime WHERE date<=? ORDER BY date DESC LIMIT 1", (asof,)).fetchone()
    if not row:
        return "市場狀態：尚未計算"
    data = json.loads(row["inputs"])
    i, s = data["inputs"], data["scores"]
    parts = []
    if i.get("vix") is not None:
        parts.append(f"VIX {i['vix']:.1f}")
    if i.get("us10y_chg20") is not None:
        parts.append(f"美債10年 20日 {i['us10y_chg20']:+.2f}")
    if i.get("dxy_chg20") is not None:
        parts.append(f"美元指數 20日 {i['dxy_chg20']:+.1%}")
    if i.get("usdtwd_chg20") is not None:
        parts.append(f"美元兌台幣 20日 {i['usdtwd_chg20']:+.1%}")
    if i.get("taiex"):
        t = i["taiex"]
        parts.append(f"加權 {t['close']:,.0f}（月線 {t['ma20']:,.0f}、季線 {t['ma60']:,.0f}）")
    if i.get("foreign_spot5_bn") is not None:
        parts.append(f"外資近5日現貨 {i['foreign_spot5_bn']:+,.0f} 億")
    if i.get("foreign_oi") is not None:
        chg = f"，5日 {i['foreign_oi_chg5']:+,.0f}" if i.get("foreign_oi_chg5") is not None else ""
        parts.append(f"外資台指期淨未平倉 {i['foreign_oi']:+,.0f} 口{chg}")
    positive = [k for k, v in s.items() if v > 0]
    negative = [k for k, v in s.items() if v < 0]
    return (f"市場狀態：{STATE_LABEL[row['state']]}（分數 {row['score']:+.0f}，{row['date']}）\n" + "；".join(parts)
            + f"\n偏多因子：{'、'.join(positive) or '無'}；偏空因子：{'、'.join(negative) or '無'}")
