"""用合成行情跑一遍：特徵 → 篩選 → 純規則組建議 → 虛擬追蹤。"""
from datetime import date, timedelta

import numpy as np

from stock_radar import recommend, screening, tracking
from stock_radar.db import connect


def _trading_days(n: int) -> list[str]:
    d, out = date(2026, 1, 5), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _seed(conn, days: list[str]) -> None:
    rng = np.random.default_rng(0)
    conn.executemany("INSERT INTO index_prices VALUES (?,?,?)", [("發行量加權股價指數", d, 20000 + i) for i, d in enumerate(days)])
    series = {
        "1101": 100 * np.cumprod(1 + rng.normal(0.004, 0.012, len(days))),   # 穩定上漲
        "2202": 100 * np.cumprod(1 + rng.normal(-0.003, 0.012, len(days))),  # 下跌
    }
    rows = []
    for sym, closes in series.items():
        conn.execute("INSERT INTO instruments VALUES (?,?,?,?,?)", (sym, f"測試{sym}", "TWSE", "測試業", None))
        for i, (d, c) in enumerate(zip(days, closes)):
            vol = 3_000_000 * (2.5 if i == len(days) - 1 else 1.0)
            rows.append((sym, d, c * 0.995, c * 1.01, c * 0.99, c, vol, vol * c, "TWSE", f"{d}T14:00:00+08:00"))
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def test_rule_pipeline(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    days = _trading_days(100)
    _seed(conn, days[:90])
    asof = days[89]
    feat = screening.compute_features(conn, asof)
    assert set(screening.universe(feat).index) == {"1101", "2202"}
    screens = screening.run_screens(feat, asof)
    assert "1101" in set(screens["symbol"]) and "2202" not in set(screens["symbol"])

    ids, assessed = recommend.run_rule_group(conn, asof, feat, screens, screening.known_at_for(asof), mode="replay")
    assert len(ids) == 1
    rec = conn.execute("SELECT * FROM recommendations WHERE id=?", (ids[0],)).fetchone()
    assert rec["symbol"] == "1101" and rec["stop"] < rec["entry_low"] < rec["entry_high"] < rec["target1"]
    assert rec["size_pct"] > 0

    # 再跑一次同一天不會重複發出
    assert recommend.run_rule_group(conn, asof, feat, screens, screening.known_at_for(asof), mode="replay")[0] == []

    # 之後幾天價格下跌到買進區間，應該成交
    fill_day = days[90]
    low = rec["entry_low"] - 0.5
    conn.execute("INSERT INTO index_prices VALUES (?,?,?)", ("發行量加權股價指數", fill_day, 20100))
    conn.execute("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("1101", fill_day, rec["entry_high"] + 1, rec["entry_high"] + 2, low, low + 1, 1e6, 1e8, "TWSE", None))
    conn.commit()
    changes = tracking.update(conn, fill_day)
    assert [c["rec_id"] for c in changes["filled"]] == ids
    t = conn.execute("SELECT * FROM tracking WHERE rec_id=?", (ids[0],)).fetchone()
    assert t["state"] == "open" and t["fill_price"] == rec["entry_high"]
