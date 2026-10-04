"""新聞處理（本機模型）：分類、抽取股票代號與評等、情緒、重要性，再做去重聚類與熱度追蹤。"""
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from . import llm
from .config import cfg
from .db import dumps
from .timeutil import iso

log = logging.getLogger(__name__)
PROMPT_VERSION = "news-v1"

Category = Literal["營收", "財報", "法說", "評等", "訂單", "產能", "併購", "股利", "增資", "政策", "總經", "產業", "處分", "人事", "其他"]


class NewsExtraction(BaseModel):
    symbols: list[str] = Field(default_factory=list, description="直接相關的台股 4 位數股票代號")
    category: Category
    sentiment: float = Field(ge=-1, le=1, description="對相關個股股價的影響，-1 極負面到 1 極正面")
    importance: float = Field(ge=0, le=1, description="對股價的重要性，例行公告接近 0")
    broker: str | None = Field(None, description="發布評等或目標價的券商或法人名稱")
    rating: str | None = Field(None, description="評等，例如 買進、中立、調升")
    target_price: float | None = Field(None, description="目標價（元）")
    summary: str = Field(description="30 字以內的中文重點")


SYSTEM = ("你是台股財經新聞分析師。根據新聞內容抽取結構化資訊。只根據新聞本身，不要推測新聞沒有提到的數字。"
          "symbols 只放新聞直接討論的公司；泛論大盤或 ETF 的新聞 symbols 留空。")


def _name_index(conn) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT symbol, name FROM instruments WHERE name IS NOT NULL").fetchall()
    return sorted(((r["name"], r["symbol"]) for r in rows if len(r["name"]) >= 2), key=lambda x: -len(x[0]))


def _hints(text: str, names: list[tuple[str, str]], limit: int = 12) -> list[str]:
    found = []
    for name, sym in names:
        if name in text and sym not in found:
            found.append(f"{name}({sym})")
            if len(found) >= limit:
                break
    return found


def _extract(conn, ev: dict, names, valid: set[str]):
    text = f"{ev['title']}\n{ev['body'] or ''}"[:2500]
    hints = _hints(text, names)
    user = f"新聞來源：{ev['source']}\n標題：{ev['title']}\n內容：{(ev['body'] or '')[:2000]}"
    if hints:
        user += f"\n\n文中出現的上市櫃公司（供參考）：{'、'.join(hints)}"
    obj, _ = llm.ask(conn, "news", SYSTEM, user, NewsExtraction, PROMPT_VERSION, thinking=False, max_tokens=500)
    if obj is None:
        return ev["id"], None
    known = set(json.loads(ev["symbols"] or "[]"))
    obj.symbols = sorted({s for s in obj.symbols if s in valid} | (known & valid))
    return ev["id"], obj


_busy = threading.Lock()


def process_pending(conn, limit: int = 300) -> int:
    """同一時間只允許一個批次，避免盤前分析與新聞排程重複處理同一批新聞。"""
    if not _busy.acquire(blocking=False):
        return 0
    try:
        return _process(conn, limit)
    finally:
        _busy.release()


def _process(conn, limit: int) -> int:
    conn.execute("UPDATE events SET processed=1 WHERE processed=0 AND kind='fed'")
    rows = [dict(r) for r in conn.execute(
        "SELECT id, source, title, body, symbols, available_at FROM events WHERE processed=0 AND kind IN ('news','material') "
        "ORDER BY id DESC LIMIT ?", (limit,))]
    if not rows:
        return 0
    names = _name_index(conn)
    valid = {s for _, s in names}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda ev: _extract(conn, ev, names, valid), rows))
    by_id = {r["id"]: r for r in rows}
    n = 0
    for ev_id, obj in results:
        if obj is None:
            conn.execute("UPDATE events SET processed=-1 WHERE id=?", (ev_id,))
            continue
        conn.execute("UPDATE events SET processed=1, symbols=?, category=?, sentiment=?, importance=?, summary=?, "
                     "broker=?, rating=?, target_price=? WHERE id=?",
                     (dumps(obj.symbols), obj.category, obj.sentiment, obj.importance, obj.summary, obj.broker,
                      obj.rating, obj.target_price, ev_id))
        assign_cluster(conn, {**by_id[ev_id], "symbols": obj.symbols, "category": obj.category,
                              "sentiment": obj.sentiment, "importance": obj.importance})
        n += 1
    conn.commit()
    return n


# ---------- 去重聚類 ----------

def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\W_]+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def assign_cluster(conn, ev: dict) -> int:
    since = iso(datetime.fromisoformat(ev["available_at"]) - timedelta(hours=cfg()["news"]["cluster_hours"]))
    syms = set(ev["symbols"])
    grams = _bigrams(ev["title"])
    best, best_score = None, 0.0
    for c in conn.execute("SELECT * FROM news_clusters WHERE last_seen>=?", (since,)):
        csyms = set(json.loads(c["symbols"]))
        if syms and csyms and not (syms & csyms):
            continue
        if bool(syms) != bool(csyms):
            continue
        score = _jaccard(grams, _bigrams(c["title"]))
        need = 0.3 if syms else 0.45
        if (c["category"] == ev["category"] and syms and score >= need * 0.7) or score >= need:
            if score > best_score:
                best, best_score = c, score
    if best:
        sources = sorted(set(json.loads(best["sources"])) | {ev["source"]})
        n = best["count"] + 1
        conn.execute("UPDATE news_clusters SET count=?, last_seen=?, sources=?, sentiment=?, importance=? WHERE id=?",
                     (n, max(best["last_seen"], ev["available_at"]), dumps(sources),
                      (best["sentiment"] * best["count"] + ev["sentiment"]) / n,
                      max(best["importance"], ev["importance"]), best["id"]))
        cid = best["id"]
    else:
        cid = conn.execute(
            "INSERT INTO news_clusters (title, symbols, category, sentiment, importance, first_seen, last_seen, count, sources) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ev["title"], dumps(sorted(syms)), ev["category"], ev["sentiment"], ev["importance"],
             ev["available_at"], ev["available_at"], 1, dumps([ev["source"]]))).lastrowid
    conn.execute("UPDATE events SET cluster_id=? WHERE id=?", (cid, ev["id"]))
    return cid


def recent_clusters(conn, known_at: str, hours: int = 24, symbol: str | None = None, min_importance: float = 0.0) -> list[dict]:
    since = iso(datetime.fromisoformat(known_at) - timedelta(hours=hours))
    rows = conn.execute("SELECT * FROM news_clusters WHERE first_seen>=? AND first_seen<=? AND importance>=? "
                        "ORDER BY importance DESC, count DESC", (since, known_at, min_importance)).fetchall()
    out = [dict(r) | {"symbols": json.loads(r["symbols"]), "sources": json.loads(r["sources"])} for r in rows]
    return [c for c in out if symbol is None or symbol in c["symbols"]]
