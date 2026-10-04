"""APScheduler 排程。台股工作用台灣時間，美股工作用美東時間（自動處理夏令時間）。"""
import logging
from datetime import datetime, timedelta
from itertools import groupby

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import jobs
from .collectors import macro
from .config import cfg
from .timeutil import NY, TW, now_tw

log = logging.getLogger(__name__)

# (工作名稱, 觸發條件)
SCHEDULE = [
    ("us_close", CronTrigger(day_of_week="mon-fri", hour=17, minute=30, timezone=NY)),
    ("tw_premarket", CronTrigger(day_of_week="mon-fri", hour=6, minute=30, timezone=TW)),
    ("tw_premarket_report", CronTrigger(day_of_week="mon-fri", hour=7, minute=45, timezone=TW)),
    ("intraday_monitor", CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/5", timezone=TW)),
    ("news_cycle", CronTrigger(hour="7-23", minute="10,25,40,55", timezone=TW)),
    ("tw_postmarket", CronTrigger(day_of_week="mon-fri", hour="14-17", minute=45, timezone=TW)),
    ("tw_evening", CronTrigger(day_of_week="mon-fri", hour=21, minute=45, timezone=TW)),
    ("macro_refresh", CronTrigger(hour="0,12", minute=5, timezone=TW)),
    ("scenario_build", CronTrigger(hour=19, minute=30, timezone=TW)),
    ("us_premarket_report", CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=NY)),
    ("revenue_collect", CronTrigger(day="1-12", hour=18, minute=0, timezone=TW)),
    ("weekly_review", CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=TW)),
]


def _quiet(name: str):
    """排程觸發時吞掉例外（已在 job_runs 記錄並推播），避免中斷排程器。"""
    def run(**kwargs):
        try:
            jobs.JOBS[name](**kwargs)
        except Exception:
            pass
    return run


def build() -> BlockingScheduler:
    sched = BlockingScheduler(timezone=TW, executors={"default": ThreadPoolExecutor(8)},
                              job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900})
    enabled = set(cfg()["schedule"]["enabled_jobs"])
    for name, trigger in SCHEDULE:
        if name in enabled:
            sched.add_job(_quiet(name), trigger, id=name, name=name)

    def schedule_macro_watch(events: list[dict]) -> None:
        """同一時間、同一來源的事件合併成一個監看工作，減少 API 呼叫。"""
        keyed = [(e["scheduled_at"], macro.event_spec(e["title"])["source"], e) for e in events
                 if e["actual_value"] is None and macro.event_spec(e["title"])["source"]]
        keyed.sort(key=lambda x: (x[0], x[1]))
        for (when, source), group in groupby(keyed, key=lambda x: (x[0], x[1])):
            ids = [g[2]["id"] for g in group]
            start = datetime.fromisoformat(when) - timedelta(minutes=2)
            if start < now_tw() - timedelta(minutes=60):
                continue
            sched.add_job(_quiet("macro_watch"), DateTrigger(max(start, now_tw() + timedelta(seconds=5))),
                          kwargs={"ids": ids}, id=f"macro_watch:{when}:{source}", replace_existing=True,
                          misfire_grace_time=3600)
            log.info("已排入總經監看 %s %s %s", when, source, ids)

    jobs.SCHEDULE_HOOK = schedule_macro_watch
    sched.add_job(_quiet("macro_refresh"), DateTrigger(now_tw() + timedelta(seconds=10)), id="macro_refresh_startup")
    return sched


def run() -> None:
    sched = build()
    for j in sched.get_jobs():
        log.info("排程：%s → %s", j.name, j.trigger)
    sched.start()
