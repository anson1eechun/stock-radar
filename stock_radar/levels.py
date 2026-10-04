"""價位引擎：入場區間、停損、停利全部由規則計算，AI 不產生數字。"""
import math

from .config import cfg


def tick_size(price: float) -> float:
    for limit, tick in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)):
        if price < limit:
            return tick
    return 5.0


def round_tick(price: float, mode: str = "nearest") -> float:
    t = tick_size(price)
    n = price / t
    n = {"nearest": round(n), "down": math.floor(n + 1e-9), "up": math.ceil(n - 1e-9)}[mode]
    return round(n * t, 2)


def entry_zone(strategy: str, close: float, ma20: float, atr: float) -> tuple[float, float]:
    if strategy == "breakout":
        return close - 0.6 * atr, close + 0.2 * atr
    if strategy == "pullback":
        center = (close + ma20) / 2
        return center - 0.35 * atr, center + 0.35 * atr
    if strategy == "event":
        return close - 0.5 * atr, close + 0.3 * atr
    return close - 0.8 * atr, close + 0.1 * atr  # trend：等拉回再買


def compute(strategy: str, horizon: str, close: float, ma20: float, atr: float) -> dict:
    lv = cfg()["levels"]
    lo, hi = entry_zone(strategy, close, ma20, atr)
    entry_low, entry_high = round_tick(lo, "down"), round_tick(hi, "up")
    if entry_high <= entry_low:
        entry_high = round_tick(entry_low + tick_size(entry_low), "up")
    mid = (entry_low + entry_high) / 2
    risk = lv["stop_atr"][horizon] * atr
    stop = round_tick(mid - risk, "down")
    if stop >= entry_low:
        stop = round_tick(entry_low - tick_size(entry_low), "down")
    r = mid - stop
    t1, t2 = (round_tick(mid + k * r, "nearest") for k in lv["target_r"])
    return {"entry_low": entry_low, "entry_high": entry_high, "stop": stop, "target1": t1, "target2": t2,
            "time_stop_days": lv["time_stop_days"][horizon], "entry_window_days": lv["entry_window_days"]}


def priced_in(close: float, ma20: float, atr: float, move_since_news_atr: float | None = None) -> float:
    """0＝尚未反映，1＝可能已反映。看股價離均線多遠，以及新聞出現後已經走了幾個 ATR。"""
    ext = max(0.0, (close - ma20) / atr) / 3
    if move_since_news_atr is not None:
        ext = max(ext, max(0.0, move_since_news_atr) / 3)
    return round(min(ext, 1.0), 2)


def priced_in_label(x: float | None) -> str:
    if x is None:
        return "—"
    return "可能已反映" if x >= 0.67 else "部分反映" if x >= 0.33 else "尚未反映"
