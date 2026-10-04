import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")


def now_tw() -> datetime:
    return datetime.now(TW)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_tw()).astimezone(TW).isoformat(timespec="seconds")


def today_tw() -> date:
    return now_tw().date()


def roc_to_date(s: str) -> date:
    """'1150923'、'115/09/23'、'115-09-23' → date(2026, 9, 23)"""
    digits = re.sub(r"\D", "", s.strip())
    return date(int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:]))


def roc_ym(s: str) -> str:
    """'11508' → '2026-08'"""
    s = s.strip()
    return f"{int(s[:-2]) + 1911:04d}-{s[-2:]}"


def to_tw(dt: datetime) -> datetime:
    return dt.astimezone(TW)


def us_cutoff_date(known_at: str) -> str:
    """在 known_at 當下，已經收盤定案的最新美股交易日（美東 16:00 收盤）。"""
    ny = datetime.fromisoformat(known_at).astimezone(NY)
    return (ny.date() if ny.hour >= 16 else ny.date() - timedelta(days=1)).isoformat()


def quarter_end(d: date) -> date:
    month = ((d.month - 1) // 3 + 1) * 3
    nxt = date(d.year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)
