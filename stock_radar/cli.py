"""命令列工具：stock-radar <指令>"""
import argparse
import logging
import subprocess
import sys

from .config import ROOT, cfg


def _setup_logging(verbose: bool) -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "data" / "stock-radar.log", encoding="utf-8")])
    for noisy in ("urllib3", "yfinance", "peewee", "apscheduler.executors", "discord"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_init(args):
    from .collectors import macro, tw_market as tm, us_market
    from .db import connect
    conn = connect()
    print("休市日", tm.collect_holidays(conn))
    b = cfg()["backfill"]
    tm.backfill(conn, args.days or b["price_days"], b["flow_days"])
    print("美股與匯率", us_market.collect(conn))
    print("月營收", tm.collect_revenue(conn), "注意/處置", tm.collect_flags(conn), "除權息", tm.collect_exdiv(conn),
          "外資期貨", tm.collect_futures_oi(conn), "重大訊息", tm.collect_material_news(conn))
    print("總經行事曆", macro.refresh_calendar(conn))


def cmd_run(args):
    from . import jobs
    kwargs = {"force": True} if args.force else {}
    if args.job == "tw_premarket" and args.skip_ai:
        kwargs["skip_ai"] = True
    print(jobs.JOBS[args.job](**kwargs))


def cmd_replay(args):
    from .db import connect
    from .recommend import replay
    print("重播產生建議", replay(connect(), args.start, args.end))


def cmd_status(args):
    from .db import connect, df
    conn = connect()
    print(df(conn, "SELECT MAX(date) AS 最新行情, COUNT(DISTINCT date) AS 交易日數 FROM prices WHERE market IN ('TWSE','TPEX')").to_string(index=False))
    print("\n最近工作：")
    print(df(conn, "SELECT job, started_at, ok, substr(message,1,80) AS message FROM job_runs ORDER BY id DESC LIMIT 15").to_string(index=False))
    print("\n建議狀態：")
    print(df(conn, "SELECT r.mode, r.grp, t.state, COUNT(*) AS n, ROUND(AVG(t.r_multiple),2) AS avg_r FROM recommendations r "
                   "JOIN tracking t ON t.rec_id=r.id GROUP BY 1,2,3").to_string(index=False))
    print("\nLLM：")
    print(df(conn, "SELECT provider, role, COUNT(*) AS n, SUM(ok) AS ok, ROUND(AVG(latency_s),1) AS sec, ROUND(SUM(cost_usd),3) AS usd "
                   "FROM analyses GROUP BY 1,2").to_string(index=False))


def cmd_action(args):
    from .db import connect
    from .timeutil import iso
    conn = connect()
    rec = conn.execute("SELECT id, symbol, name, entry_high FROM recommendations WHERE id=?", (args.rec_id,)).fetchone()
    if not rec:
        sys.exit(f"找不到建議 #{args.rec_id}")
    price = args.price if args.price is not None else (rec["entry_high"] if args.action == "took" else None)
    conn.execute("INSERT INTO user_actions (rec_id, action, price, at, note) VALUES (?,?,?,?,?)",
                 (args.rec_id, args.action, price, iso(), args.note))
    conn.commit()
    print(f"已記錄 #{rec['id']} {rec['symbol']} {rec['name']}：{args.action} {price or ''}")


def cmd_event_study(args):
    from .context import event_study
    from .db import connect
    conn = connect()
    print("FOMC 升降息", event_study.backfill_fomc(conn))
    if args.fred:
        print("FRED 首次公布值", event_study.backfill_fred(conn))
    print("系統累積", event_study.update_live(conn))
    for key in ("FOMC", "NFP", "CPI", "CLAIMS"):
        print(event_study.summary_text(conn, key))


def cmd_discord_test(args):
    from .delivery import discord
    ok = discord.send("✅ stock-radar 測試訊息：請點下方按鈕，應該會看到「按鈕連線正常」", buttons=[("測試按鈕", "noop:0", 2)])
    if ok:
        print("已送出")
    elif discord.configured():
        print("發送失敗，詳見上方錯誤訊息")
    else:
        print("未設定 Discord（訊息已寫入 data/outbox.log）")


def main():
    p = argparse.ArgumentParser(prog="stock-radar", description="台股事件驅動選股系統")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="初始化：回補行情、美股、營收、行事曆")
    s.add_argument("--days", type=int)
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("run", help="手動執行一個排程工作")
    s.add_argument("job")
    s.add_argument("--force", action="store_true", help="忽略休市日與時段限制")
    s.add_argument("--skip-ai", action="store_true")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("replay", help="用歷史資料重播純規則組")
    s.add_argument("--start", required=True)
    s.add_argument("--end")
    s.set_defaults(fn=cmd_replay)
    sub.add_parser("status", help="系統狀態").set_defaults(fn=cmd_status)
    for action, help_text in (("took", "回報已進場"), ("skip", "回報略過"), ("exit", "回報已出場")):
        s = sub.add_parser(action, help=help_text)
        s.add_argument("rec_id", type=int)
        s.add_argument("--price", type=float)
        s.add_argument("--note")
        s.set_defaults(fn=cmd_action, action=action)
    s = sub.add_parser("event-study", help="建立總經事件歷史反應資料庫")
    s.add_argument("--fred", action="store_true", help="用 FRED 回補歷史首次公布值（需 FRED_API_KEY）")
    s.set_defaults(fn=cmd_event_study)
    sub.add_parser("discord-test", help="送出測試訊息").set_defaults(fn=cmd_discord_test)
    sub.add_parser("scheduler", help="啟動排程器").set_defaults(fn=lambda a: __import__("stock_radar.scheduler", fromlist=["run"]).run())
    sub.add_parser("bot", help="啟動 Discord 按鈕 Bot").set_defaults(fn=lambda a: __import__("stock_radar.delivery.discord_bot", fromlist=["run"]).run())
    sub.add_parser("dashboard", help="啟動儀表板").set_defaults(
        fn=lambda a: subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard" / "app.py"),
                                     "--server.address", "127.0.0.1", "--server.port", "8501",
                                     "--server.headless", "true", "--browser.gatherUsageStats", "false"]))
    args = p.parse_args()
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
