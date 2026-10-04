"""排程工作：把收集、分析、追蹤、推播串起來。每個工作都記錄在 job_runs，失敗會推播通知。"""
import functools
import logging
import time
import traceback
from datetime import datetime, timedelta

from . import analysis, monitor, news_processing, recommend, scenarios, screening, stats, tracking
from .collectors import macro, news, tw_market as tm, us_market
from .config import cfg
from .context import event_study, regime, supply_chain
from .db import connect, kv_get, kv_set
from .delivery import discord, reports
from .timeutil import TW, iso, now_tw, today_tw

log = logging.getLogger(__name__)
JOBS: dict = {}
SCHEDULE_HOOK = None  # scheduler 啟動時設定，用來動態排入總經監看工作


def job(name: str):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            conn = connect()
            prev = conn.execute("SELECT ok FROM job_runs WHERE job=? AND ok IS NOT NULL ORDER BY id DESC LIMIT 1",
                                (name,)).fetchone()
            prev_failed = prev is not None and prev["ok"] == 0
            run_id = conn.execute("INSERT INTO job_runs (job, started_at) VALUES (?,?)", (name, iso())).lastrowid
            conn.commit()
            try:
                msg = fn(conn, *args, **kwargs)
                conn.execute("UPDATE job_runs SET finished_at=?, ok=1, message=? WHERE id=?",
                             (iso(), str(msg)[:2000] if msg is not None else None, run_id))
                if prev_failed:
                    discord.send(f"✅ 排程工作 `{name}` 已恢復正常")
                return msg
            except Exception as e:
                log.exception("工作 %s 失敗", name)
                conn.execute("UPDATE job_runs SET finished_at=?, ok=0, message=? WHERE id=?",
                             (iso(), traceback.format_exc()[-2000:], run_id))
                conn.commit()
                # 只在「由正常轉為失敗」時通知一次，避免斷網時每 5 分鐘洗版
                if kwargs.get("notify_errors", True) and not prev_failed:
                    discord.send(f"⚠️ 排程工作 `{name}` 失敗（恢復後會再通知）：{type(e).__name__}: {e}")
                raise
            finally:
                conn.commit()
                conn.close()
        JOBS[name] = wrapper
        return wrapper
    return deco


def _trading_today(conn, force: bool) -> bool:
    return force or tm.is_trading_day(conn, today_tw())


# ---------- 資料收集 ----------

@job("us_close")
def us_close(conn, **_):
    return f"{us_market.collect(conn, start=(today_tw() - timedelta(days=20)).isoformat())} 筆"


@job("tw_evening")
def tw_evening(conn, force: bool = False, **_):
    out = {}
    if _trading_today(conn, force):
        missing = tuple(p for p, table in (("inst", "institutional"), ("margin", "margin"))
                        if not tm.has_table_rows(conn, table, today_tw()))
        if missing:
            out["flows"] = tm.collect_flows(conn, today_tw(), missing)
    out["flags"] = tm.collect_flags(conn)
    out["exdiv"] = tm.collect_exdiv(conn)
    out["futures_oi"] = tm.collect_futures_oi(conn)
    out["material"] = tm.collect_material_news(conn)
    if now_tw().day == 1 or not conn.execute("SELECT 1 FROM holidays LIMIT 1").fetchone():
        out["holidays"] = tm.collect_holidays(conn)
    return out


@job("news_cycle")
def news_cycle(conn, **_):
    got = news.collect_all(conn)
    got["material"] = tm.collect_material_news(conn)
    processed = news_processing.process_pending(conn)
    alerts = monitor.check_invalidation(conn)
    for a in alerts:
        discord.send(monitor.format_invalidation(a))
    return {"collected": got, "processed": processed, "invalidation_alerts": len(alerts)}


@job("revenue_collect")
def revenue_collect(conn, **_):
    n = tm.collect_revenue(conn)
    if n == 0:
        return "無新營收"
    asof = screening.latest_trading_date(conn)
    feat = screening.universe(screening.compute_features(conn, asof, iso()))
    if "rev_yoy" not in feat:
        return f"新增 {n} 筆"
    hot = feat[(feat["rev_yoy"] >= 50) & (feat["rev_cum_yoy"] >= 20)].sort_values("rev_yoy", ascending=False)
    if len(hot):
        lines = [f"📦 **月營收亮點**（新增 {n} 筆，年增 ≥ 50% 且累計年增 ≥ 20%）"]
        lines += [f"・{s} {r['name']}（{r['industry']}）{r['rev_ym']} 年增 {r['rev_yoy']:.0f}%，累計 {r['rev_cum_yoy']:.0f}%，月增 {r['rev_mom']:+.0f}%"
                  for s, r in hot.head(15).iterrows()]
        discord.send("\n".join(lines))
    return f"新增 {n} 筆，亮點 {len(hot)} 檔"


# ---------- 台股盤前 ----------

@job("tw_premarket")
def tw_premarket(conn, force: bool = False, skip_ai: bool = False, **_):
    today = today_tw()
    if not _trading_today(conn, force):
        return "休市"
    known_at = iso()
    try:
        news.collect_all(conn)
        tm.collect_material_news(conn)
        news_processing.process_pending(conn)
    except Exception as e:
        log.warning("盤前新聞處理失敗，繼續執行：%s", e)
    asof = screening.latest_trading_date(conn, (today - timedelta(days=1)).isoformat())
    regime.compute(conn, asof, known_at)
    feat = screening.compute_features(conn, asof, known_at)
    screens = screening.run_screens(feat, asof)
    screening.save_candidates(conn, asof, screens, feat)
    rule_ids, _ = recommend.run_rule_group(conn, asof, feat, screens, known_at)
    ai_ids, reviewed = ([], []) if skip_ai else analysis.run_ai_group(conn, asof, feat, screens, known_at)
    kv_set(conn, f"premarket:{today}", {"asof": asof, "rule": rule_ids, "ai": ai_ids, "reviewed": reviewed,
                                        "known_at": known_at, "n_candidates": int(screens["symbol"].nunique())})
    return f"資料日 {asof}，候選 {screens['symbol'].nunique()} 檔，純規則組 {rule_ids}，AI 組 {ai_ids}"


@job("tw_premarket_report")
def tw_premarket_report(conn, force: bool = False, **_):
    today = today_tw()
    data = kv_get(conn, f"premarket:{today}")
    if not data:
        return "今天沒有盤前分析結果（休市或盤前分析失敗）"
    known_at = iso()
    start = datetime.combine(today, datetime.min.time(), TW)
    lines = [f"📊 **台股盤前報告 {today}**（資料截至 {data['asof']}）", "",
             regime.describe(conn, data["asof"]), "", supply_chain.overnight_text(conn, known_at)]
    try:
        digests = supply_chain.recent_earnings(conn, known_at)
    except Exception as e:
        log.warning("美國財報摘要失敗：%s", e)
        digests = []
    for d in digests:
        lines.append(f"財報｜{d['name']}（{d['date']}）：{d['summary']}\n→ 台灣供應鏈：{d['tw_implication']}（信心 {d['confidence']}）")
    lines += ["", reports.macro_section(conn, iso(start), iso(start + timedelta(hours=30)), "今明重要數據")]
    top = news_processing.recent_clusters(conn, known_at, hours=18, min_importance=0.7)[:6]
    if top:
        lines += ["", "【重要新聞】"] + [f"・[{c['category']}] {c['title']}（{c['count']} 則，情緒 {c['sentiment']:+.1f}）" for c in top]
    watch = [r for r in data.get("reviewed", []) if r["decision"] == "watch"]
    lines += ["", f"【新建議】AI 組 {len(data['ai'])} 檔、純規則組 {len(data['rule'])} 檔（候選 {data.get('n_candidates', '—')} 檔）"]
    if watch:
        lines.append("AI 觀察名單：" + "、".join(f"{w['symbol']} {w['name']}" for w in watch))
    lines += ["", reports.positions_section(conn)]
    discord.send("\n".join(lines))
    for r in reports.recs_for(conn, data["ai"] + data["rule"]):
        discord.send(reports.rec_card(r), buttons=[("✅ 已進場", f"took:{r['id']}", 3), ("⏭ 略過", f"skip:{r['id']}", 2)])
    return f"已推播 {len(data['ai']) + len(data['rule'])} 筆建議"


# ---------- 盤中與盤後 ----------

@job("intraday_monitor")
def intraday_monitor(conn, force: bool = False, **_):
    now = now_tw()
    if not force and (not tm.is_trading_day(conn, now.date()) or not ("09:00" <= now.strftime("%H:%M") <= "13:35")):
        return "非交易時段"
    alerts = monitor.price_alerts(conn)
    if alerts:
        discord.send("\n".join(alerts))
    return f"{len(alerts)} 則警報"


@job("tw_postmarket")
def tw_postmarket(conn, force: bool = False, **_):
    today = today_tw()
    if not _trading_today(conn, force):
        return "休市"
    if kv_get(conn, f"postmarket_done:{today}") and not force:
        return "今天已完成"
    if not tm.has_quotes(conn, today):
        if tm.collect_quotes(conn, today) == 0:
            return "行情尚未公布，稍後重試"
    if not tm.has_table_rows(conn, "institutional", today):
        tm.collect_flows(conn, today, ("inst",))
    changes = tracking.update(conn, today.isoformat())
    try:
        event_study.update_live(conn)
    except Exception as e:
        log.warning("事件反應資料庫更新失敗：%s", e)
    lines = [f"📈 **台股盤後報告 {today}**", "", reports.changes_section(changes), "",
             reports.positions_section(conn), "", reports.performance_section(conn)]
    discord.send("\n".join(lines))
    kv_set(conn, f"postmarket_done:{today}", True)
    return {k: len(v) for k, v in changes.items()}


# ---------- 總經 ----------

@job("macro_refresh")
def macro_refresh(conn, **_):
    n = macro.refresh_calendar(conn)
    upcoming = macro.key_events_between(conn, now_tw() - timedelta(minutes=30), now_tw() + timedelta(hours=48))
    if SCHEDULE_HOOK:
        SCHEDULE_HOOK(upcoming)
    return f"行事曆 {n} 筆，未來 48 小時重要事件 {len(upcoming)} 筆"


def macro_flash_text(conn, u: dict) -> str:
    cls = {"above": "高於預期", "below": "低於預期", "inline": "符合預期"}.get(u["surprise_class"], "無預期值可比較")
    lines = [f"⚡ **{u['title']}**：實際 {u['actual_value']:g}（預期 {u['forecast'] or '—'}，前值 {u['previous'] or '—'}）→ **{cls}**"]
    sc = scenarios.get(conn, u["id"])
    if sc and u["surprise_class"] in sc["content"]:
        scenarios.mark_matched(conn, u["id"], u["surprise_class"])
        lines += ["", "命中劇本：", scenarios.format_case(u["surprise_class"], sc["content"][u["surprise_class"]])]
    hist = event_study.summary(conn, u["event_key"])
    if not hist.empty:
        h = hist[(hist["bucket"] == u["surprise_class"]) & (hist["target"] == "^TWII")]
        if len(h):
            r = h.iloc[0]
            lines.append(f"歷史：同類情況 {int(r['n'])} 次，隔日加權平均 {r['mean_1d']:+.2%}，上漲機率 {r['up_rate']:.0%}")
    return "\n".join(lines)


@job("macro_watch")
def macro_watch(conn, ids: list[int], **_):
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM macro_calendar WHERE id IN ({','.join('?' * len(ids))})", ids)]
    pending = [r for r in rows if r["actual_value"] is None and macro.event_spec(r["title"])["source"]]
    if not pending:
        return "沒有需要監看的事件"
    source = macro.event_spec(pending[0]["title"])["source"]
    scheduled = datetime.fromisoformat(pending[0]["scheduled_at"])
    deadline = scheduled + timedelta(minutes=cfg()["macro"]["watch_minutes"][source])
    interval = {"bls": 60, "fred": 120, "fed": 30}[source]
    if now_tw() < scheduled:
        time.sleep((scheduled - now_tw()).total_seconds())
    got = []
    while pending and now_tw() < deadline:
        try:
            for u in macro.check_actuals(conn, pending):
                discord.send(macro_flash_text(conn, u))
                got.append(u["title"])
        except Exception as e:
            log.warning("檢查實際值失敗：%s", e)
        pending = [dict(r) for r in conn.execute(
            f"SELECT * FROM macro_calendar WHERE id IN ({','.join('?' * len(ids))}) AND actual_value IS NULL", ids)
            if macro.event_spec(r["title"])["source"]]
        if pending:
            time.sleep(interval)
    if pending:
        discord.send("⏱ " + "、".join(r["title"] for r in pending) + f" 在 {cfg()['macro']['watch_minutes'][source]} 分鐘內未取得實際值")
    return f"取得：{got}；未取得：{[r['title'] for r in pending]}"


@job("scenario_build")
def scenario_build(conn, **_):
    built = []
    for m in scenarios.due(conn):
        if scenarios.build(conn, m):
            discord.send(scenarios.format_set(m, scenarios.get(conn, m["id"])["content"]))
            built.append(m["title"])
    return f"產出劇本：{built}"


@job("us_premarket_report")
def us_premarket_report(conn, **_):
    known_at = iso()
    now = now_tw()
    lines = [f"🇺🇸 **美股盤前報告 {now:%Y-%m-%d}**", "",
             reports.macro_section(conn, iso(now - timedelta(hours=12)), iso(now + timedelta(hours=10)), "今晚美國數據")]
    try:
        import yfinance as yf
        fut = []
        for sym, label in (("ES=F", "標普期貨"), ("NQ=F", "那斯達克期貨"), ("^SOX", "費半前收")):
            fi = yf.Ticker(sym).fast_info
            fut.append(f"{label} {fi['last_price'] / fi['previous_close'] - 1:+.2%}")
        lines += ["", "【期貨】" + "、".join(fut)]
    except Exception as e:
        log.warning("取得美股期貨失敗：%s", e)
    try:
        dates = supply_chain.earnings_dates(conn)
        soon = [f"{supply_chain.graph()[s]['name']}（{d}）" for s, ds in dates.items() for d in ds
                if now.date().isoformat() <= d <= (now.date() + timedelta(days=2)).isoformat()]
        if soon:
            lines += ["", "【供應鏈美國企業近期財報】" + "、".join(soon)]
    except Exception as e:
        log.warning("取得財報日期失敗：%s", e)
    held = reports.active_positions(conn)
    linked = [f"{p['symbol']} {p['name']}：{supply_chain.links_text(conn, p['symbol'], known_at)}" for p in held
              if supply_chain.links_text(conn, p["symbol"], known_at)]
    if linked:
        lines += ["", "【持有建議的美國連動】"] + [f"・{x}" for x in linked]
    discord.send("\n".join(lines))
    return "已推播"


# ---------- 每週 ----------

@job("weekly_review")
def weekly_review(conn, **_):
    stats.update_signal_stats(conn)
    week_ago = iso(now_tw() - timedelta(days=7))
    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM analyses WHERE created_at>=?", (week_ago,)).fetchone()
    fails = conn.execute("SELECT job, COUNT(*) FROM job_runs WHERE ok=0 AND started_at>=? GROUP BY job", (week_ago,)).fetchall()
    lines = [f"🗓 **每週檢討 {today_tw()}**", "", reports.performance_section(conn), "", stats.stats_text(conn), "",
             stats.behavior_text(conn), "", f"【系統】本週 LLM 呼叫 {cost[1]} 次，雲端花費 ${cost[0]:.2f}"]
    if fails:
        lines.append("失敗的工作：" + "、".join(f"{f[0]} {f[1]} 次" for f in fails))
    discord.send("\n".join(lines))
    return "已推播"
