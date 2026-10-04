"""重大事件前的情境劇本：公布前產出三套劇本，公布後比對實際命中哪一套。"""
import json
import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from . import llm
from .context import event_study, regime, supply_chain
from .db import dumps
from .delivery.reports import active_positions
from .timeutil import iso, now_tw

log = logging.getLogger(__name__)
PROMPT_VERSION = "scenario-v1"
CASE_LABEL = {"above": "高於預期", "inline": "符合預期", "below": "低於預期"}


class Scenario(BaseModel):
    definition: str = Field(description="觸發條件，例如「實際值 > 0.3%」")
    market_read: str = Field(description="市場可能的解讀，60 字以內")
    tw_positive: list[str] = Field(default_factory=list, max_length=5, description="可能受惠的台股類股或個股")
    tw_negative: list[str] = Field(default_factory=list, max_length=5, description="可能受壓的台股類股或個股")
    actions: list[str] = Field(min_length=1, max_length=4, description="對既有建議與觀察清單的具體行動")
    watch_symbols: list[str] = Field(default_factory=list, max_length=6, description="要特別觀察的台股代號")


class ScenarioSet(BaseModel):
    above: Scenario
    inline: Scenario
    below: Scenario
    summary: str = Field(description="這次數據的關注重點，60 字以內")


SYSTEM = ("你是總經策略師，專門判斷美國總經數據對台股的影響。根據預期值、前值、歷史反應統計、市場狀態與目前持有的建議，"
          "預先寫好三套劇本。三套劇本的定義要以數值區間明確劃分，彼此互斥。歷史統計樣本少時要保守，不可誇大。")


def build(conn, macro: dict) -> int | None:
    from .config import cfg
    spec = cfg()["macro"]["events"].get(macro["title"], {})
    tol = spec.get("tol", 0)
    known_at = iso()
    positions = active_positions(conn)
    pos_text = "；".join(f"{p['symbol']} {p['name']}（{'持有' if p['state'] == 'open' else '等待進場'}）" for p in positions) or "無"
    user = (f"事件：{macro['title']}（{macro['event_key']}），台灣時間 {macro['scheduled_at'][:16]} 公布\n"
            f"市場預期：{macro['forecast'] or '無'}；前值：{macro['previous'] or '無'}；"
            f"系統判定「符合預期」的容忍範圍：預期值 ±{tol}\n\n"
            f"{event_study.summary_text(conn, macro['event_key'])}\n\n"
            f"{regime.describe(conn, known_at[:10])}\n\n{supply_chain.overnight_text(conn, known_at)}\n\n"
            f"目前系統建議：{pos_text}")
    obj, analysis_id = llm.ask(conn, "scenario", SYSTEM, user, ScenarioSet, PROMPT_VERSION, thinking=True, max_tokens=2500)
    if obj is None:
        return None
    cur = conn.execute("INSERT OR REPLACE INTO scenarios (macro_id, event_key, created_at, content, analysis_id) VALUES (?,?,?,?,?)",
                       (macro["id"], macro["event_key"], known_at, dumps(obj.model_dump()), analysis_id))
    conn.commit()
    return cur.lastrowid


def due(conn, hours: int = 36) -> list[dict]:
    """未來 hours 小時內、有市場預期值、還沒有劇本的重要事件。"""
    end = iso(now_tw() + timedelta(hours=hours))
    rows = conn.execute(
        "SELECT m.* FROM macro_calendar m LEFT JOIN scenarios s ON s.macro_id=m.id WHERE m.event_key IS NOT NULL "
        "AND m.forecast_value IS NOT NULL AND m.scheduled_at>? AND m.scheduled_at<=? AND s.id IS NULL "
        "AND m.impact='High'", (iso(), end)).fetchall()
    return [dict(r) for r in rows]


def get(conn, macro_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM scenarios WHERE macro_id=?", (macro_id,)).fetchone()
    return {**dict(row), "content": json.loads(row["content"])} if row else None


def mark_matched(conn, macro_id: int, case: str) -> None:
    conn.execute("UPDATE scenarios SET matched=?, matched_at=? WHERE macro_id=?", (case, iso(), macro_id))
    conn.commit()


def format_set(macro: dict, content: dict) -> str:
    lines = [f"🎯 **情境劇本｜{macro['title']}**（台灣時間 {macro['scheduled_at'][5:16].replace('T', ' ')}）",
             f"預期 {macro['forecast'] or '—'}｜前值 {macro['previous'] or '—'}", content["summary"], ""]
    for case in ("above", "inline", "below"):
        lines.append(format_case(case, content[case]))
        lines.append("")
    return "\n".join(lines)


def format_case(case: str, s: dict) -> str:
    out = [f"**{CASE_LABEL[case]}**：{s['definition']}", f"解讀：{s['market_read']}"]
    if s["tw_positive"]:
        out.append("受惠：" + "、".join(s["tw_positive"]))
    if s["tw_negative"]:
        out.append("受壓：" + "、".join(s["tw_negative"]))
    out.append("行動：" + "；".join(a.rstrip("。；") for a in s["actions"]))
    if s["watch_symbols"]:
        out.append("觀察：" + "、".join(s["watch_symbols"]))
    return "\n".join(out)
