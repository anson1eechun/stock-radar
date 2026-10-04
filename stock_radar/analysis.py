"""AI 分析：脈絡組裝 → 多方/空方論證 → 仲裁 → AI 組建議。價位仍由價位引擎計算。"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from . import levels, llm, news_processing, recommend, risk, screening
from .config import cfg
from .db import df, dumps
from .timeutil import iso

log = logging.getLogger(__name__)
PROMPT_VERSION = "debate-v3"


class Case(BaseModel):
    arguments: list[str] = Field(min_length=1, max_length=5, description="論點，每點 60 字以內，必須引用脈絡中的資料")
    confidence: float = Field(ge=0, le=1, description="這一方論點的說服力")


class BearCase(Case):
    invalidation_triggers: list[str] = Field(default_factory=list, max_length=4,
                                             description="若做多，哪些可觀察的事件或價格會證明判斷錯誤")


class Decision(BaseModel):
    action: Literal["buy_pullback", "buy_now", "wait", "reject"] = Field(description=(
        "buy_pullback＝值得做多，在系統買進區間掛單等拉回（搭配 pullback 或 trend）；"
        "buy_now＝值得做多，現價附近進場（搭配 breakout 或 event）；"
        "wait＝需要等特定新資訊（財報、數據公布）才能判斷；reject＝不值得做多"))
    horizon: Literal["swing", "position"] = Field(description="swing＝數天到數週，position＝數週到數月")
    strategy: Literal["breakout", "pullback", "trend", "event"]
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(description="150 字以內的做多理由，需引用脈絡中的資料")
    invalidation: list[str] = Field(min_length=1, max_length=4, description="具體、可觀察的失效條件")
    key_risks: list[str] = Field(default_factory=list, max_length=4)


BULL_SYSTEM = ("你是台股多方研究員。根據提供的資料，提出最有力的做多理由。只能使用脈絡中的資料，不可編造數字或新聞；"
               "資料不足時要誠實降低 confidence。")
BEAR_SYSTEM = ("你是台股空方研究員（風險審查）。根據提供的資料，找出做多這檔股票最大的風險與反對理由，"
               "並列出若做多時哪些可觀察的條件代表判斷錯誤。只能使用脈絡中的資料，不可編造。")
ARBITER_SYSTEM = (
    "你是獨立的投資仲裁者，負責台股波段與中長線的做多建議（只做多，不做空）。閱讀市場脈絡、個股資料、多方與空方論證，"
    "以及系統過往績效，選擇 action：buy_pullback、buy_now、wait 或 reject。\n"
    "原則：1. 證據不夠強就不要買。2. 已大幅反映的利多要降低評價，偏向 buy_pullback 而不是 buy_now。"
    "3. 失效條件必須具體可觀察（價格、籌碼、營收或特定事件），不要寫空泛的句子。"
    "4. 入場與停損價位由系統計算，你只需選策略模板：breakout（突破追價）、pullback（拉回承接）、trend（順勢等拉回）、event（事件驅動）。"
    "個股資料中列有各模板的系統價位。\n"
    "5. 買進的實際執行方式是在買進區間掛限價單，數日未成交自動失效。pullback 與 trend 的區間低於現價，要等拉回才成交。"
    "所以「基本面與籌碼偏多、但短線漲多或大盤不穩，想等拉回再承接」就是 buy_pullback。"
    "wait 只用於需要等待特定新資訊才能判斷的情況。")


def _fmt_pct(x, digits=1):
    return "—" if x is None or pd.isna(x) else f"{x:+.{digits}%}"


def market_context(conn, asof: str, known_at: str) -> str:
    from .context import regime, supply_chain
    lines = [f"資料日期：{asof}（決策時間 {known_at[:16]}）"]
    lines.append(regime.describe(conn, asof))
    lines.append(supply_chain.overnight_text(conn, known_at))
    end = (datetime.fromisoformat(known_at) + timedelta(days=3)).isoformat()
    events = conn.execute("SELECT title, scheduled_at, forecast, previous FROM macro_calendar WHERE event_key IS NOT NULL "
                          "AND scheduled_at>=? AND scheduled_at<=? ORDER BY scheduled_at", (known_at, end)).fetchall()
    if events:
        lines.append("未來 3 日美國重要數據：" + "；".join(
            f"{e['scheduled_at'][5:16]} {e['title']}（預期 {e['forecast'] or '—'}，前值 {e['previous'] or '—'}）" for e in events))
    top = news_processing.recent_clusters(conn, known_at, hours=24, min_importance=0.6)[:8]
    if top:
        lines.append("過去 24 小時重要新聞：\n" + "\n".join(
            f"- [{c['category']}｜情緒 {c['sentiment']:+.1f}｜{c['count']} 則] {c['title']}" for c in top))
    return "\n".join(lines)


def stock_context(conn, feat: pd.DataFrame, symbol: str, screens: list[str], known_at: str) -> str:
    from .context import supply_chain
    f = screening.feature_dict(feat, symbol)
    row = feat.loc[symbol]
    lines = [
        f"股票：{symbol} {row.get('name')}（{row.get('industry') or '產業不明'}，{row.get('market')}）",
        f"價格：收盤 {f['close']:g}，月線 {f['ma20']:.2f}，季線 {f['ma60']:.2f}，ATR {f['atr']:.2f}（{f['atr_pct']:.1%}），"
        f"5 日 {_fmt_pct(f.get('ret5'))}，20 日 {_fmt_pct(f.get('ret20'))}，RSI {f.get('rsi14', 0):.0f}，量比 {f.get('vol_ratio', 0):.1f}",
    ]
    if "foreign_net5" in f:
        lines.append(f"籌碼：外資近 5 日 {f['foreign_net5'] / 1000:+,.0f} 張，投信近 5 日 {f.get('trust_net5', 0) / 1000:+,.0f} 張，"
                     f"法人買超 {int(f.get('inst_buy_days5', 0))}/5 日")
    m = conn.execute("SELECT date, margin_balance FROM margin WHERE symbol=? ORDER BY date DESC LIMIT 6", (symbol,)).fetchall()
    if len(m) >= 2 and m[-1]["margin_balance"]:
        lines.append(f"融資餘額：{m[0]['margin_balance']:,.0f} 張（{len(m) - 1} 日變化 {m[0]['margin_balance'] / m[-1]['margin_balance'] - 1:+.1%}）")
    if "rev_yoy" in f:
        lines.append(f"月營收：{f['rev_ym']} 年增 {f['rev_yoy']:.1f}%，月增 {f.get('rev_mom', 0):.1f}%，累計年增 {f.get('rev_cum_yoy', 0):.1f}%")
    lines.append(f"命中篩選：{'、'.join(screening.SCREEN_LABEL.get(s, s) for s in screens)}")
    zones = []
    for tpl in ("pullback", "trend", "breakout"):
        lv = levels.compute(tpl, "swing", f["close"], f["ma20"], f["atr"])
        zones.append(f"{tpl} 買進 {lv['entry_low']:g}–{lv['entry_high']:g}、停損 {lv['stop']:g}、目標 {lv['target1']:g}/{lv['target2']:g}")
    lines.append("系統價位（波段）：" + "；".join(zones))
    lines.append(f"反映程度：{levels.priced_in_label(levels.priced_in(f['close'], f['ma20'], f['atr']))}（股價距月線 {f['dist_ma20_atr']:.1f} 個 ATR）")
    news = news_processing.recent_clusters(conn, known_at, hours=24 * 7, symbol=symbol)[:6]
    if news:
        lines.append("近 7 日新聞：\n" + "\n".join(
            f"- {c['first_seen'][5:10]} [{c['category']}｜情緒 {c['sentiment']:+.1f}｜{c['count']} 則] {c['title']}" for c in news))
    targets = conn.execute("SELECT broker, rating, target_price, available_at FROM events WHERE symbols LIKE ? AND target_price IS NOT NULL "
                           "AND available_at<=? ORDER BY available_at DESC LIMIT 3", (f'%"{symbol}"%', known_at)).fetchall()
    if targets:
        lines.append("法人目標價：" + "；".join(f"{t['broker'] or '法人'} {t['rating'] or ''} {t['target_price']:g}（{t['available_at'][5:10]}）"
                                          for t in targets))
    links = supply_chain.links_text(conn, symbol, known_at)
    if links:
        lines.append(links)
    return "\n".join(lines)


def track_record(conn, screens: list[str]) -> str:
    rows = conn.execute(f"SELECT kind, key, n, win_rate, avg_r, status FROM signal_stats WHERE (kind='screen' AND key IN "
                        f"({','.join('?' * len(screens))})) OR (kind='group' AND key='ai')", screens).fetchall()
    if not rows:
        return "系統過往績效：尚無足夠紀錄"
    return "系統過往績效：" + "；".join(f"{r['key']} {r['n']} 筆，勝率 {r['win_rate']:.0%}，平均 {r['avg_r']:+.2f}R（{r['status']}）" for r in rows)


def news_catalyst_screens(conn, feat: pd.DataFrame, known_at: str) -> pd.DataFrame:
    uni = set(screening.universe(feat).index)
    rows = []
    for c in news_processing.recent_clusters(conn, known_at, hours=24, min_importance=0.6):
        if c["sentiment"] < 0.5 or c["category"] in ("其他", "人事", "總經"):
            continue
        for s in c["symbols"]:
            if s in uni:
                rows.append({"symbol": s, "screen": "news_catalyst", "score": round(c["importance"] * c["sentiment"], 3)})
    out = pd.DataFrame(rows, columns=["symbol", "screen", "score"])
    return out.sort_values("score", ascending=False).drop_duplicates("symbol") if not out.empty else out


def debate(conn, market_ctx: str, stock_ctx: str, record: str) -> tuple[Decision | None, int | None, dict]:
    base = f"【市場脈絡】\n{market_ctx}\n\n【個股資料】\n{stock_ctx}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        fb = pool.submit(llm.ask, conn, "bull", BULL_SYSTEM, base, Case, PROMPT_VERSION, True, 1200)
        fr = pool.submit(llm.ask, conn, "bear", BEAR_SYSTEM, base, BearCase, PROMPT_VERSION, True, 1200)
        (bull, _), (bear, _) = fb.result(), fr.result()
    if bull is None or bear is None:
        return None, None, {}
    user = (f"{base}\n\n【多方論證】（說服力 {bull.confidence:.2f}）\n" + "\n".join(f"- {a}" for a in bull.arguments)
            + f"\n\n【空方論證】（說服力 {bear.confidence:.2f}）\n" + "\n".join(f"- {a}" for a in bear.arguments)
            + ("\n空方提出的失效條件：" + "；".join(bear.invalidation_triggers) if bear.invalidation_triggers else "")
            + f"\n\n【{record}】")
    decision, analysis_id = llm.ask(conn, "arbiter", ARBITER_SYSTEM, user, Decision, PROMPT_VERSION, True, 1500)
    return decision, analysis_id, {"bull": bull.model_dump(), "bear": bear.model_dump()}


def run_ai_group(conn, asof: str, feat: pd.DataFrame, screens: pd.DataFrame, known_at: str,
                 mode: str = "live", issued_at: str | None = None) -> tuple[list[int], list[dict]]:
    sc = cfg()["screening"]
    catalysts = news_catalyst_screens(conn, feat, known_at)
    if not catalysts.empty:
        screening.save_candidates(conn, asof, catalysts, feat)
    all_screens = pd.concat([screens, catalysts], ignore_index=True)
    ranked = screening.rank(all_screens, tuple(screening.STRATEGY), recommend.deprecated_screens(conn))
    ranked = ranked.head(sc["ai_candidates"])
    if ranked.empty:
        return [], []
    market_ctx = market_context(conn, asof, known_at)
    # 資料庫讀取在主執行緒完成；執行緒裡只呼叫 LLM（llm 模組寫入稽核紀錄時有加鎖）
    items = [(sym, r, stock_context(conn, feat, sym, r["screens"], known_at), track_record(conn, r["screens"]))
             for sym, r in ranked.iterrows()]

    def evaluate(item):
        sym, r, ctx, record = item
        decision, analysis_id, _ = debate(conn, market_ctx, ctx, record)
        return sym, r, decision, analysis_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(evaluate, items))

    picks, reviewed = [], []
    for sym, r, d, analysis_id in results:
        buy = d is not None and d.action in ("buy_pullback", "buy_now") and d.confidence >= sc["ai_min_confidence"]
        if d is None:
            decision = "error"
        elif buy:
            decision = "recommend"
        elif d.action == "reject":
            decision = "reject"
        else:
            decision = "watch"
        reviewed.append({"symbol": sym, "name": feat.loc[sym].get("name"), "screens": r["screens"], "decision": decision,
                         "action": d.action if d else None, "confidence": d.confidence if d else None,
                         "thesis": d.thesis if d else None})
        if not buy:
            continue
        strategy = d.strategy
        if d.action == "buy_pullback" and strategy not in ("pullback", "trend"):
            strategy = "pullback"
        elif d.action == "buy_now" and strategy not in ("breakout", "event"):
            strategy = "breakout"
        p = recommend.build_pick(feat, sym, r["screen"], r["score"], strategy, d.horizon)
        news = [c for c in news_processing.recent_clusters(conn, known_at, 72, sym)]
        if news:
            first = min(c["first_seen"] for c in news)[:10]
            pre = conn.execute("SELECT close FROM prices WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1", (sym, first)).fetchone()
            if pre:
                f = p["features"]
                p["priced_in"] = levels.priced_in(f["close"], f["ma20"], f["atr"], (f["close"] - pre["close"]) / f["atr"])
        p.update(confidence=d.confidence, thesis=d.thesis, invalidation=d.invalidation, analysis_id=analysis_id,
                 sources=[f"screen:{s}" for s in r["screens"]] + sorted({f"news:{c['category']}" for c in news}))
        picks.append(p)
    picks.sort(key=lambda p: -p["confidence"])
    assessed = risk.assess(conn, picks, "ai", mode, asof, known_at)
    accepted = [p for p in assessed if not p["excluded"]][: sc["ai_top_k"]]
    ids = recommend.issue(conn, "ai", mode, asof, issued_at or iso(), accepted)
    return ids, reviewed
