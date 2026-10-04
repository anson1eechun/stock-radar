"""stock-radar 儀表板：streamlit run dashboard/app.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from stock_radar import stats
from stock_radar.context import regime
from stock_radar.db import connect, df
from stock_radar.delivery.reports import GRP_LABEL, REASON_LABEL, rec_card
from stock_radar.timeutil import iso, today_tw

st.set_page_config(page_title="stock-radar", layout="wide")
conn = connect()
mode = st.sidebar.radio("資料", ["live", "replay"], format_func=lambda m: {"live": "實際運行", "replay": "歷史重播（純規則組）"}[m])
st.sidebar.caption("歷史重播只用當時已知的資料；AI 組無法重播。")

tab_recs, tab_perf, tab_events, tab_regime, tab_actions, tab_sys = st.tabs(
    ["建議", "績效", "事件與新聞", "市場狀態", "交易回報", "系統"])

with tab_recs:
    active = df(conn, "SELECT r.id, r.grp, r.symbol, r.name, r.strategy, r.horizon, r.entry_low, r.entry_high, r.stop, "
                      "r.target1, r.target2, t.state, t.fill_date, t.fill_price, t.stop_current, t.t1_hit, "
                      "(SELECT close FROM prices p WHERE p.symbol=r.symbol ORDER BY date DESC LIMIT 1) AS last_close "
                      "FROM recommendations r JOIN tracking t ON t.rec_id=r.id WHERE r.mode=? AND t.state IN ('pending','open') "
                      "ORDER BY t.state, r.id DESC", (mode,))
    if not active.empty:
        active["未實現R"] = ((active["last_close"] - active["fill_price"]) / (active["fill_price"] - active["stop"])).round(2)
        active["grp"] = active["grp"].map(GRP_LABEL)
    st.subheader(f"進行中（{len(active)}）")
    st.dataframe(active, hide_index=True, width="stretch")
    st.subheader("最近發出的建議")
    recent = df(conn, "SELECT * FROM recommendations WHERE mode=? ORDER BY id DESC LIMIT 30", (mode,))
    for r in recent.to_dict("records"):
        with st.expander(f"#{r['id']} {r['asof_date']} {r['symbol']} {r['name']}（{GRP_LABEL.get(r['grp'])}）"):
            st.markdown(rec_card(r).replace("\n", "  \n"))

with tab_perf:
    closed = stats.closed_trades(conn, (mode,))
    if closed.empty:
        st.info("尚無結案建議")
    else:
        closed = closed.sort_values("exit_date")
        summary = closed.groupby("grp")["r_multiple"].agg(筆數="size", 勝率=lambda x: (x > 0).mean(), 平均R="mean", 合計R="sum")
        summary.index = summary.index.map(GRP_LABEL)
        st.dataframe(summary.style.format({"勝率": "{:.0%}", "平均R": "{:+.2f}", "合計R": "{:+.1f}"}))
        curve = closed.assign(組別=closed["grp"].map(GRP_LABEL)).pivot_table(
            index="exit_date", columns="組別", values="r_multiple", aggfunc="sum").fillna(0).cumsum()
        st.subheader("累積 R")
        st.line_chart(curve)
        by_reason = closed.groupby("exit_reason")["r_multiple"].agg(["size", "mean"])
        by_reason.index = by_reason.index.map(lambda k: REASON_LABEL.get(k, k))
        st.subheader("出場原因")
        st.dataframe(by_reason)
    st.subheader("訊號績效與淘汰狀態")
    if st.button("重新計算"):
        stats.update_signal_stats(conn)
    st.dataframe(df(conn, "SELECT * FROM signal_stats ORDER BY kind, n DESC"), hide_index=True, width="stretch")

with tab_events:
    st.subheader("總經行事曆")
    st.dataframe(df(conn, "SELECT scheduled_at, title, impact, forecast, previous, actual_value, surprise_class "
                          "FROM macro_calendar WHERE event_key IS NOT NULL OR impact='High' ORDER BY scheduled_at DESC LIMIT 40"),
                 hide_index=True, width="stretch")
    st.subheader("情境劇本")
    for s in df(conn, "SELECT s.*, m.title FROM scenarios s JOIN macro_calendar m ON m.id=s.macro_id ORDER BY s.id DESC LIMIT 5").to_dict("records"):
        with st.expander(f"{s['title']}（{s['created_at'][:16]}）命中：{s['matched'] or '尚未公布'}"):
            st.json(json.loads(s["content"]))
    st.subheader("新聞事件（48 小時，依重要性）")
    st.dataframe(df(conn, "SELECT first_seen, category, round(importance,2) AS importance, round(sentiment,2) AS sentiment, "
                          "count, symbols, title FROM news_clusters ORDER BY first_seen DESC LIMIT 200")
                 .sort_values("importance", ascending=False), hide_index=True, width="stretch")

with tab_regime:
    latest = conn.execute("SELECT MAX(date) FROM market_regime").fetchone()[0]
    st.text(regime.describe(conn, latest) if latest else "尚未計算")
    hist = df(conn, "SELECT date, score FROM market_regime ORDER BY date")
    if not hist.empty:
        st.line_chart(hist.set_index("date"))
    taiex = df(conn, "SELECT date, close AS 加權指數 FROM index_prices WHERE name='發行量加權股價指數' ORDER BY date")
    st.line_chart(taiex.set_index("date"))

with tab_actions:
    st.subheader("回報進場／略過／出場")
    opts = df(conn, "SELECT r.id, r.symbol, r.name, r.entry_high, t.state FROM recommendations r JOIN tracking t ON t.rec_id=r.id "
                    "WHERE r.mode='live' ORDER BY r.id DESC LIMIT 100")
    if opts.empty:
        st.info("尚無實際運行的建議")
    else:
        with st.form("action"):
            choice = st.selectbox("建議", opts.to_dict("records"),
                                  format_func=lambda r: f"#{r['id']} {r['symbol']} {r['name']}（{r['state']}）")
            action = st.radio("動作", ["took", "skip", "exit"], horizontal=True,
                              format_func=lambda a: {"took": "已進場", "skip": "略過", "exit": "已出場"}[a])
            price = st.number_input("價格（略過可留 0）", min_value=0.0, value=float(choice["entry_high"]), step=0.05)
            note = st.text_input("備註")
            if st.form_submit_button("送出"):
                conn.execute("INSERT INTO user_actions (rec_id, action, price, at, note) VALUES (?,?,?,?,?)",
                             (choice["id"], action, price or None, iso(), note or "dashboard"))
                conn.commit()
                st.success("已記錄")
    st.text(stats.behavior_text(conn))
    st.dataframe(df(conn, "SELECT * FROM user_actions ORDER BY id DESC LIMIT 50"), hide_index=True, width="stretch")

with tab_sys:
    st.subheader("工作紀錄")
    st.dataframe(df(conn, "SELECT job, started_at, finished_at, ok, substr(message,1,200) AS message FROM job_runs ORDER BY id DESC LIMIT 60"),
                 hide_index=True, width="stretch")
    st.subheader("LLM 使用量")
    st.dataframe(df(conn, "SELECT substr(created_at,1,10) AS day, provider, role, COUNT(*) AS calls, SUM(ok) AS ok, "
                          "ROUND(AVG(latency_s),1) AS avg_sec, ROUND(SUM(cost_usd),3) AS usd FROM analyses GROUP BY 1,2,3 ORDER BY 1 DESC"),
                 hide_index=True, width="stretch")
