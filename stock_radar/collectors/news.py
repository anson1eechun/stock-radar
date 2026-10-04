"""新聞來源：Yahoo 股市 RSS、鉅亨網、Fed 新聞稿。重大訊息在 tw_market.collect_material_news。"""
import hashlib
import html
import logging
import re
from datetime import datetime, timezone

import feedparser

from ..config import cfg
from ..db import dumps
from ..http import get_json, get_text
from ..timeutil import iso

log = logging.getLogger(__name__)


def _clean(s: str | None, limit: int = 1500) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _insert(conn, uid, kind, source, license_class, title, body, url, symbols, published) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (uid, kind, source, license_class, title, body, url, symbols, published_at, available_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uid, kind, source, license_class, title, body, url, dumps(symbols), published, iso()))
    return cur.rowcount


def _rss(conn, url: str, source: str, kind: str, license_class: str) -> int:
    feed = feedparser.parse(get_text(url))
    n = 0
    for e in feed.entries:
        published = iso(datetime(*e.published_parsed[:6], tzinfo=timezone.utc)) if e.get("published_parsed") else iso()
        uid = f"{source}:" + hashlib.sha1(e.link.encode()).hexdigest()[:16]
        n += _insert(conn, uid, kind, source, license_class, _clean(e.title, 300), _clean(e.get("summary")),
                     e.link, [], published)
    conn.commit()
    return n


def collect_cnyes(conn) -> int:
    data = get_json(cfg()["news"]["sources"]["cnyes_api"])
    n = 0
    for it in data["items"]["data"]:
        symbols = [s for s in (it.get("stock") or []) if re.fullmatch(r"[1-9]\d{3}", str(s))]
        body = _clean(it.get("summary")) + " " + _clean(it.get("content"), 1200)
        published = iso(datetime.fromtimestamp(int(it["publishAt"]), tz=timezone.utc))
        n += _insert(conn, f"cnyes:{it['newsId']}", "news", "cnyes", "media", _clean(it["title"], 300), body.strip(),
                     f"https://news.cnyes.com/news/id/{it['newsId']}", symbols, published)
    conn.commit()
    return n


def collect_all(conn) -> dict:
    src = cfg()["news"]["sources"]
    out = {}
    jobs = {"yahoo": lambda: _rss(conn, src["yahoo_rss"], "yahoo", "news", "media"),
            "cnyes": lambda: collect_cnyes(conn),
            "fed": lambda: _rss(conn, src["fed_rss"], "fed", "fed", "public")}
    for name, fn in jobs.items():
        try:
            out[name] = fn()
        except Exception as e:
            log.warning("新聞來源 %s 失敗：%s", name, e)
            out[name] = f"錯誤：{e}"
    return out


def google_news(query: str, lang: str = "zh-TW", limit: int = 8) -> list[dict]:
    """查詢特定主題的新聞標題（不寫入資料庫，用於即時補充脈絡）。"""
    from urllib.parse import quote
    region = {"zh-TW": "hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "en": "hl=en-US&gl=US&ceid=US:en"}[lang]
    feed = feedparser.parse(get_text(f"https://news.google.com/rss/search?q={quote(query)}&{region}"))
    return [{"title": _clean(e.title, 200), "published": e.get("published", ""), "link": e.link} for e in feed.entries[:limit]]
