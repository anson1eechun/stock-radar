import pandas as pd
import pytest

from stock_radar import levels, tracking
from stock_radar.collectors import macro
from stock_radar.delivery.discord import split
from stock_radar.llm import LLMError, extract_json
from stock_radar.screening import rank
from stock_radar.timeutil import roc_to_date, roc_ym, us_cutoff_date


def test_tick_rounding():
    assert levels.tick_size(9.99) == 0.01
    assert levels.tick_size(50) == 0.1
    assert levels.tick_size(1000) == 5
    assert levels.round_tick(123.3, "nearest") == 123.5
    assert levels.round_tick(123.3, "down") == 123.0
    assert levels.round_tick(1003, "up") == 1005
    assert levels.round_tick(48.12, "down") == 48.10


def test_levels_ordering():
    lv = levels.compute("pullback", "swing", close=100, ma20=98, atr=4)
    assert lv["stop"] < lv["entry_low"] < lv["entry_high"] < lv["target1"] < lv["target2"]
    mid = (lv["entry_low"] + lv["entry_high"]) / 2
    assert mid - lv["stop"] == pytest.approx(8, abs=0.6)  # 2 ATR


def test_priced_in():
    assert levels.priced_in(100, 100, 5) == 0
    assert levels.priced_in(115, 100, 5) == 1.0
    assert levels.priced_in(100, 100, 5, move_since_news_atr=1.5) == 0.5


REC = {"entry_low": 95, "entry_high": 100, "stop": 90, "target1": 115, "target2": 130,
       "entry_window_days": 3, "time_stop_days": 5}
PENDING = {"state": "pending", "invalidated_at": None}


def bar(d, o, h, l, c):
    return {"date": d, "open": o, "high": h, "low": l, "close": c}


def test_fill_at_limit_and_stop():
    t = tracking.step(REC, PENDING, bar("2026-01-02", 102, 103, 99, 101), 1, 0)
    assert t["state"] == "open" and t["fill_price"] == 100
    t = tracking.step(REC, t, bar("2026-01-05", 95, 96, 89, 90), 2, 1)
    assert t["state"] == "closed" and t["exit_reason"] == "stop" and t["r_multiple"] == pytest.approx(-1.0)


def test_gap_open_fill_and_gap_stop():
    t = tracking.step(REC, PENDING, bar("2026-01-02", 97, 99, 96, 98), 1, 0)
    assert t["fill_price"] == 97
    t = tracking.step(REC, t, bar("2026-01-05", 85, 86, 84, 85), 2, 1)
    assert t["exit_price"] == 85 and t["r_multiple"] == pytest.approx((85 - 97) / 7, abs=1e-3)


def test_targets_and_breakeven():
    t = tracking.step(REC, PENDING, bar("2026-01-02", 100, 101, 99, 100), 1, 0)
    t = tracking.step(REC, t, bar("2026-01-05", 110, 116, 109, 114), 2, 1)
    assert t["t1_hit"] == 1 and t["stop_current"] == 100 and t["realized_r"] == pytest.approx(0.75)
    t = tracking.step(REC, t, bar("2026-01-06", 112, 113, 99, 100), 3, 2)
    assert t["exit_reason"] == "breakeven" and t["r_multiple"] == pytest.approx(0.75)


def test_target2():
    t = tracking.step(REC, PENDING, bar("2026-01-02", 100, 101, 99, 100), 1, 0)
    t = tracking.step(REC, t, bar("2026-01-05", 114, 131, 113, 129), 2, 1)
    assert t["exit_reason"] == "target2" and t["r_multiple"] == pytest.approx(0.75 + 1.5)


def test_expire_and_time_stop():
    t = tracking.step(REC, PENDING, bar("2026-01-08", 120, 121, 119, 120), 4, 0)
    assert t["state"] == "expired"
    t = tracking.step(REC, PENDING, bar("2026-01-02", 100, 101, 99, 100), 1, 0)
    t = tracking.step(REC, t, bar("2026-01-09", 104, 106, 103, 105), 6, 5)
    assert t["exit_reason"] == "time" and t["r_multiple"] == pytest.approx(0.5)


def test_invalidation_exit():
    t = tracking.step(REC, PENDING, bar("2026-01-02", 100, 101, 99, 100), 1, 0)
    t["invalidated_at"] = "2026-01-05T07:30:00+08:00"
    t = tracking.step(REC, t, bar("2026-01-05", 98, 99, 96, 97), 2, 1)
    assert t["exit_reason"] == "invalidated" and t["exit_price"] == 98
    p = dict(PENDING, invalidated_at="2026-01-01T20:00:00+08:00")
    assert tracking.step(REC, p, bar("2026-01-02", 100, 101, 99, 100), 1, 0)["state"] == "cancelled"


def test_macro_parsing():
    assert macro.parse_value("201K") == 201
    assert macro.parse_value("-0.1%") == -0.1
    assert macro.parse_value("1.2M") == 1200
    assert macro.parse_value("") is None
    assert macro.surprise_class(0.4, 0.3, 0.05) == "above"
    assert macro.surprise_class(0.3, 0.3, 0.05) == "inline"
    assert macro.surprise_class(180, 201, 8) == "below"
    obs = [("2026-07", 100.0), ("2026-08", 100.4)]
    assert macro.compute("pct_mom", obs) == ("2026-08", 0.4)
    assert macro.compute("diff", obs) == ("2026-08", pytest.approx(0.4))
    assert macro.compute("thousands", [("2026-09-19", 201000.0)]) == ("2026-09-19", 201.0)


def test_fed_statement_parsing():
    hold = "decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent."
    hike = "decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent"
    cut = "lower the target range for the federal funds rate by 1/2 percentage point to 4-3/4 to 5 percent"
    assert macro.parse_fed_upper(hold) == 3.75
    assert macro.parse_fed_upper(hike) == 4.0
    assert macro.parse_fed_upper(cut) == 5.0


def test_roc_dates():
    assert roc_to_date("1150923").isoformat() == "2026-09-23"
    assert roc_to_date("115/09/23").isoformat() == "2026-09-23"
    assert roc_ym("11508") == "2026-08"


def test_us_cutoff():
    assert us_cutoff_date("2026-09-24T06:30:00+08:00") == "2026-09-23"
    assert us_cutoff_date("2026-09-24T02:00:00+08:00") == "2026-09-22"


def test_discord_split():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    chunks = split(text, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks) == text


def test_extract_json():
    assert extract_json('<think>hmm</think>\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('結果如下：{"a": [1, 2]} 以上') == {"a": [1, 2]}
    with pytest.raises(LLMError):
        extract_json("沒有 JSON")


def test_rank_bonus_for_multiple_screens():
    s = pd.DataFrame([{"symbol": "1111", "screen": "breakout", "score": 0.6},
                      {"symbol": "1111", "screen": "pullback", "score": 0.5},
                      {"symbol": "2222", "screen": "pullback", "score": 0.65}])
    r = rank(s, ("breakout", "pullback"))
    assert list(r.index) == ["1111", "2222"]
    assert r.loc["1111", "score"] == pytest.approx(0.7)
    assert rank(s, ("pullback",), {"pullback"}).empty
